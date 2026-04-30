"""
JavaScript / TypeScript AST query functions powered by tree-sitter.

Handles both .js/.jsx/.mjs (JavaScript) and .ts/.tsx (TypeScript).

Modes:
  classes      - List class, interface (TS), enum (TS) declarations
  methods      - List function and method definitions
  calls        - Find call sites of FUNC
  implements   - Find classes that extend/implement BASE
  declarations - Find declaration(s) by name
  all_refs     - Find every identifier occurrence
  imports      - List import statements
  params       - Show parameter list of FUNC
  attrs        - List decorators (TS), optionally filtered by NAME
"""

EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
TS_EXTENSIONS = frozenset({".ts", ".tsx"})
TSX_EXTENSIONS = frozenset({".tsx"})

import sys
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser
from ._util import (_make_matches, FileDescription,
                    JsClassInfo, JsMethodInfo, JsImportInfo)

_JS_LANG  = Language(tsjs.language())
_js_parser  = Parser(_JS_LANG)
_TS_LANG  = Language(tsts.language_typescript())
_ts_parser  = Parser(_TS_LANG)
_TSX_LANG = Language(tsts.language_tsx())
_tsx_parser = Parser(_TSX_LANG)

# ── Inlined from src/ast/js.py ───────────────────────────────────────────────

_TYPE_DECL_NODES = {
    "class_declaration",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "abstract_class_declaration",
}

_FUNCTION_NODES = {
    "function_declaration",
    "method_definition",
    "arrow_function",
    "generator_function_declaration",
}

_LITERAL_NODES = {
    "comment",
    "string",
    "template_string",
    "regex",
}


def _find_all(node, predicate, results=None):
    if results is None:
        results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if predicate(n):
            results.append(n)
        stack.extend(reversed(n.children))
    return results


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _LITERAL_NODES:
            return True
        p = p.parent
    return False


def _line(node) -> int:
    return node.start_point[0] + 1


def _class_bases(node, src: bytes) -> list:
    """Get base/implemented type names from a class declaration."""
    names = []
    for child in node.children:
        if child.type in ("class_heritage", "extends_clause", "implements_clause"):
            for c in _find_all(child, lambda n: n.type in ("identifier", "type_identifier")):
                t = _text(c, src).strip()
                if t not in ("extends", "implements") and t:
                    names.append(t)
        elif child.type == "extends_clause":
            for c in child.named_children:
                if c.type in ("identifier", "type_identifier"):
                    names.append(_text(c, src).strip())
    return names


def _fn_name_from_node(node, src: bytes) -> str:
    """Get function name from function_declaration or method_definition."""
    n = node.child_by_field_name("name")
    if n:
        return _text(n, src).strip()
    for c in node.children:
        if c.type in ("identifier", "property_identifier", "string"):
            return _text(c, src).strip()
    return ""


def _fn_sig(node, src: bytes) -> str:
    """Build a readable signature for a function/method node."""
    name = _fn_name_from_node(node, src)
    params_node = node.child_by_field_name("parameters")
    ret_node = node.child_by_field_name("return_type")
    params_txt = _text(params_node, src).strip() if params_node else "()"
    ret_txt = f": {_text(ret_node, src).strip()}" if ret_node else ""
    return f"{name}{params_txt}{ret_txt}"


# ── Data extraction functions ─────────────────────────────────────────────────

def _js_q_classes_data(src, tree) -> list:
    """Return list[JsClassInfo] for all class/interface/enum declarations."""
    results = []
    type_nodes = {
        "class_declaration", "abstract_class_declaration",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
    }
    for node in _find_all(tree.root_node, lambda n: n.type in type_nodes):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name  = _text(name_node, src).strip()
        kind  = (node.type
                 .replace("_declaration", "")
                 .replace("_alias", " alias")
                 .replace("abstract_", "abstract "))
        bases = _class_bases(node, src)
        results.append(JsClassInfo(line=_line(node), name=name, kind=kind, bases=bases))
    return results


def _js_q_methods_data(src, tree) -> list:
    """Return list[JsMethodInfo] for all function/method definitions."""
    results = []
    fn_types = {
        "function_declaration", "generator_function_declaration",
        "method_definition",
    }
    for node in _find_all(tree.root_node, lambda n: n.type in fn_types):
        sig  = _fn_sig(node, src)
        if not sig:
            continue
        kind = "method" if node.type == "method_definition" else "function"
        name = _fn_name_from_node(node, src) or ""
        p    = node.parent
        cls_name = ""
        while p:
            if p.type in ("class_declaration", "abstract_class_declaration"):
                nn = p.child_by_field_name("name")
                if nn:
                    cls_name = _text(nn, src).strip()
                break
            p = p.parent
        results.append(JsMethodInfo(line=_line(node), name=name, kind=kind,
                                    sig=sig, cls_name=cls_name))
    return results


def _js_q_all_call_sites_data(src, tree) -> list:
    """Extract all call site names for indexing."""
    names = []
    for node in _find_all(tree.root_node, lambda n: n.type == "call_expression"):
        fn = node.child_by_field_name("function")
        if fn:
            if fn.type == "identifier":
                names.append(_text(fn, src).strip())
            elif fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                if prop:
                    names.append(_text(prop, src).strip())
    return names


def _js_q_imports_data(src, tree) -> list:
    """Return list[JsImportInfo] for all import statements."""
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("import_statement", "import_declaration")):
        full = _text(node, src).strip()
        module = ""
        src_node = node.child_by_field_name("source")
        if src_node:
            raw = _text(src_node, src).strip().strip("'\"")
            module = raw.lstrip("./").split("/")[0]
        results.append(JsImportInfo(line=_line(node), text=full, module=module))
    return results


# ── Query functions ───────────────────────────────────────────────────────────

def js_q_classes(src, tree, lines):
    """List class / interface / enum declarations."""
    return [(_r.line, _r.text) for _r in _js_q_classes_data(src, tree)]


def js_q_methods(src, tree, lines):
    """List function declarations and class method definitions."""
    return [(_r.line, _r.text) for _r in _js_q_methods_data(src, tree)]


def js_q_calls(src, tree, lines, func_name):
    """Find call sites of FUNC."""
    if "." in func_name:
        qualifier, bare_name = func_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, func_name

    results = []
    seen_rows = set()

    for node in _find_all(tree.root_node, lambda n: n.type == "call_expression"):
        if _in_literal(node):
            continue
        fn = node.child_by_field_name("function")
        if not fn:
            continue
        matched = None
        if fn.type == "identifier":
            if qualifier is None:
                matched = _text(fn, src).strip()
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            obj  = fn.child_by_field_name("object")
            if prop:
                matched = _text(prop, src).strip()
                if qualifier and obj:
                    obj_txt = _text(obj, src).strip()
                    if not (obj_txt == qualifier or obj_txt.endswith("." + qualifier)):
                        matched = None

        if matched == bare_name:
            row = node.start_point[0]
            if row not in seen_rows:
                seen_rows.add(row)
                raw = _text(node, src).replace("\n", " ")
                if len(raw) > 140:
                    raw = raw[:140] + "…"
                results.append((_line(node), raw))

    # new ClassName(...)
    if qualifier is None:
        for node in _find_all(tree.root_node, lambda n: n.type == "new_expression"):
            if _in_literal(node):
                continue
            ctor = node.child_by_field_name("constructor")
            if not ctor:
                continue
            name = _text(ctor, src).strip()
            if name == bare_name:
                row = node.start_point[0]
                if row not in seen_rows:
                    seen_rows.add(row)
                    raw = _text(node, src).replace("\n", " ")
                    if len(raw) > 140:
                        raw = raw[:140] + "…"
                    results.append((_line(node), raw))

    return results


def js_q_implements(src, tree, lines, base_name):
    """Find classes that extend or implement BASE."""
    results = []
    class_types = {"class_declaration", "abstract_class_declaration"}
    for node in _find_all(tree.root_node, lambda n: n.type in class_types):
        bases = _class_bases(node, src)
        if base_name not in bases:
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        suffix = ", ".join(bases)
        results.append((_line(node), f"[class] {name} : {suffix}"))
    return results


def js_q_declarations(src, tree, lines, name, include_body=False):
    """Find declaration(s) named NAME."""
    decl_types = {
        "function_declaration", "generator_function_declaration",
        "class_declaration", "abstract_class_declaration",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
    }
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in decl_types):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        kind = node.type.replace("_declaration", "").replace("_alias", " alias")
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        if include_body:
            content = "\n".join(lines[start_row:end_row + 1])
        else:
            body_node = node.child_by_field_name("body")
            if body_node:
                sig_end = body_node.start_point[0]
                content = "\n".join(lines[start_row:sig_end]).rstrip()
            else:
                content = "\n".join(lines[start_row:end_row + 1])
        header = f"── [{kind}] {name}  (lines {start_row + 1}–{end_row + 1}) ──"
        results.append((_line(node), f"{header}\n{content}"))
    return results


def js_q_all_refs(src, tree, lines, name):
    """Find every occurrence of NAME as an identifier."""
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type in ("identifier", "type_identifier")):
        if _text(node, src).strip() != name:
            continue
        if _in_literal(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def js_q_imports(src, tree, lines):
    """List import statements."""
    return [(_r.line, _r.text) for _r in _js_q_imports_data(src, tree)]


def js_q_params(src, tree, lines, func_name):
    """Show parameter list of FUNC."""
    results = []
    fn_types = {"function_declaration", "generator_function_declaration", "method_definition"}
    for node in _find_all(tree.root_node, lambda n: n.type in fn_types):
        if _fn_name_from_node(node, src) != func_name:
            continue
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in params_node.named_children:
            param_lines.append(f"  {_text(p, src).strip()}")
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


def js_q_attrs(src, tree, lines, attr_name=None):
    """List decorators (TypeScript). Optionally filter by NAME."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "decorator"):
        full = _text(node, src).strip()
        # @name or @name(...) or @ns.name(...)
        bare = full.lstrip("@").split("(")[0].split(".")[-1].strip()
        if attr_name and bare != attr_name:
            continue
        results.append((_line(node), full))
    return results


# ── Process function ──────────────────────────────────────────────────────────

def process_js_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Parse a JS/TS file and return list[{"line": N, "text": "..."}] for the given mode."""
    import os as _os
    ext = _os.path.splitext(path)[1].lower()
    parser = _tsx_parser if ext in TSX_EXTENSIONS else (
        _ts_parser if ext in TS_EXTENSIONS else _js_parser)

    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: js_q_classes(src_bytes, tree, lines),
        "methods":      lambda: js_q_methods(src_bytes, tree, lines),
        "calls":        lambda: js_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: js_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: js_q_declarations(src_bytes, tree, lines, mode_arg,
                                                  include_body=include_body),
        "all_refs":     lambda: js_q_all_refs(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: js_q_imports(src_bytes, tree, lines),
        "params":       lambda: js_q_params(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: js_q_attrs(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if fn is None:
        raise ValueError(f"Unknown mode: {mode!r}")
    return _make_matches(fn() or [])


def describe_js_file(path: str) -> FileDescription:
    """Parse path once and return all structured JS/TS data as a FileDescription."""
    import os as _os
    ext = _os.path.splitext(path)[1].lower()
    parser = _tsx_parser if ext in TSX_EXTENSIONS else (
        _ts_parser if ext in TS_EXTENSIONS else _js_parser)
    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="js")
    try:
        tree = parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="js")
    return FileDescription(
        path=path, language="js",
        classes=_js_q_classes_data(src_bytes, tree),
        methods=_js_q_methods_data(src_bytes, tree),
        imports=_js_q_imports_data(src_bytes, tree),
    )
