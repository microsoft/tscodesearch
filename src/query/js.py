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

from ..ast.js import (
    _find_all, _text, _in_literal, _line,
    _TYPE_DECL_NODES, _FUNCTION_NODES, _LITERAL_NODES,
    _class_bases, _fn_name_from_node, _fn_sig,
)


# ── Query functions ───────────────────────────────────────────────────────────

def js_q_classes(src, tree, lines):
    """List class / interface / enum declarations."""
    results = []
    type_nodes = {
        "class_declaration", "abstract_class_declaration",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
    }
    for node in _find_all(tree.root_node, lambda n: n.type in type_nodes):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        kind = (node.type
                .replace("_declaration", "")
                .replace("_alias", " alias")
                .replace("abstract_", "abstract "))
        bases = _class_bases(node, src)
        suffix = f" : {', '.join(bases)}" if bases else ""
        results.append((_line(node), f"[{kind}] {name}{suffix}"))
    return results


def js_q_methods(src, tree, lines):
    """List function declarations and class method definitions."""
    results = []
    fn_types = {
        "function_declaration", "generator_function_declaration",
        "method_definition",
    }
    for node in _find_all(tree.root_node, lambda n: n.type in fn_types):
        sig = _fn_sig(node, src)
        if not sig:
            continue
        kind = "method" if node.type == "method_definition" else "function"
        # enclosing class
        p = node.parent
        cls_name = ""
        while p:
            if p.type in ("class_declaration", "abstract_class_declaration"):
                nn = p.child_by_field_name("name")
                if nn:
                    cls_name = _text(nn, src).strip()
                break
            p = p.parent
        prefix = f"[in {cls_name}] " if cls_name else ""
        results.append((_line(node), f"[{kind}] {prefix}{sig}"))
    return results


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
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("import_statement", "import_declaration")):
        results.append((_line(node), _text(node, src).strip()))
    return results


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

def process_js_file(path, mode, mode_arg, show_path, count_only, context=0,
                    src_root=None, include_body=False, **kwargs):
    import os
    from tree_sitter import Language, Parser
    ext = os.path.splitext(path)[1].lower()
    if ext in TS_EXTENSIONS:
        lang = Language(tsts.language_tsx() if ext in TSX_EXTENSIONS else tsts.language_typescript())
    else:
        lang = Language(tsjs.language())
    parser = Parser(lang)

    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return 0
    try:
        tree = parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return 0

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
    if not fn:
        print(f"Unknown mode for JS/TS: {mode!r}", file=sys.stderr)
        return 0

    results = fn()
    if not results:
        return 0

    from .config import SRC_ROOT as _SRC_ROOT
    _effective_root = (src_root or _SRC_ROOT).rstrip("/").replace("\\", "/")
    _path_norm = path.replace("\\", "/")
    if _effective_root and _path_norm.lower().startswith(_effective_root.lower() + "/"):
        _disp_base = _path_norm[len(_effective_root) + 1:]
    else:
        _disp_base = _path_norm

    if count_only:
        print(f"{len(results):4d}  {_disp_base}")
        return len(results)

    for line_num_str, text in results:
        if show_path:
            print(f"{_disp_base}:{line_num_str}: {text}")
        else:
            print(f"{line_num_str}: {text}")

        if context > 0 and mode != "declarations":
            try:
                row = int(line_num_str) - 1
                start = max(0, row - context)
                end   = min(len(lines), row + context + 1)
                for i, ln in enumerate(lines[start:end], start):
                    if i == row:
                        continue
                    prefix = f"  {_disp_base}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {ln}")
                print()
            except (ValueError, IndexError):
                pass
    return len(results)
