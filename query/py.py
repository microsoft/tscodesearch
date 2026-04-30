"""
Python AST query functions — extracted from query.py.

All public functions are re-exported from query.py for backward compatibility.
"""

EXTENSIONS = frozenset({".py"})

import sys
import tree_sitter_python as tspython
from tree_sitter import Language, Parser
from ._util import _make_matches, FileDescription, ClassInfo, MethodInfo, AttrInfo, ImportInfo

from .cs import _find_all, _text

_PY_LANG = Language(tspython.language())
_py_parser = Parser(_PY_LANG)

# ── Inlined from src/ast/py.py ──────────────────────────────────────────────

_PY_LITERAL_NODES = {"comment", "string", "concatenated_string"}


def _line(node) -> int:
    return node.start_point[0] + 1


def _py_in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _PY_LITERAL_NODES:
            return True
        p = p.parent
    return False


def _py_enclosing_class(node, src) -> str:
    p = node.parent
    while p:
        if p.type == "class_definition":
            nn = p.child_by_field_name("name")
            if nn:
                return _text(nn, src).strip()
        p = p.parent
    return ""


def _py_base_names(node, src) -> list:
    names = []
    superclasses = node.child_by_field_name("superclasses")
    if not superclasses:
        return names
    for child in superclasses.named_children:
        if child.type == "identifier":
            names.append(_text(child, src).strip())
        elif child.type == "attribute":
            attr = child.child_by_field_name("attribute")
            if attr:
                names.append(_text(attr, src).strip())
        elif child.type == "subscript":
            val = child.child_by_field_name("value")
            if val and val.type == "identifier":
                names.append(_text(val, src).strip())
    return names


# ── Data extraction functions ─────────────────────────────────────────────────

def _py_q_classes_data(src, tree) -> list:
    """Return list[ClassInfo] for all class definitions."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name  = _text(name_node, src).strip()
        bases = _py_base_names(node, src)
        results.append(ClassInfo(line=_line(node), name=name, kind="class", bases=bases))
    return results


def _py_q_methods_data(src, tree) -> list:
    """Return list[MethodInfo] for all function definitions."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name_node   = node.child_by_field_name("name")
        if not name_node:
            continue
        name        = _text(name_node, src).strip()
        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        params_str  = _text(params_node, src).strip() if params_node else "()"
        ret_str     = _text(return_node, src).strip() if return_node else ""
        cls         = _py_enclosing_class(node, src)
        kind        = "method" if cls else "def"
        ret_suffix  = f" {ret_str}" if ret_str else ""
        sig         = f"def {name}{params_str}{ret_suffix}"
        param_types = []
        if params_node:
            for p in params_node.named_children:
                if p.type in ("typed_parameter", "typed_default_parameter"):
                    pt = p.child_by_field_name("type")
                    if pt:
                        param_types.append(_text(pt, src).strip())
        results.append(MethodInfo(line=_line(node), name=name, kind=kind,
                                    sig=sig, cls_name=cls or "",
                                    return_type=ret_str, param_types=param_types))
    return results


def _py_q_attrs_data(src, tree, name=None) -> list:
    """Return list[AttrInfo] for all decorators."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "decorator"):
        full  = _text(node, src).strip()
        dname = full.lstrip("@").split("(")[0].split(".")[-1].strip()
        if name and dname != name:
            continue
        results.append(AttrInfo(line=_line(node), text=full, attr_name=dname))
    return results


def _py_q_imports_data(src, tree) -> list:
    """Return list[ImportInfo] for all import statements."""
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("import_statement", "import_from_statement",
                                               "future_import_statement")):
        full = _text(node, src).strip()
        module = ""
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    module = _text(child, src).split(".")[0]
                    break
                elif child.type == "aliased_import" and child.named_children:
                    module = _text(child.named_children[0], src).split(".")[0]
                    break
        elif node.type == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m:
                module = _text(m, src).lstrip(".").split(".")[0]
        results.append(ImportInfo(line=_line(node), text=full, module=module))
    return results


def _py_q_all_call_sites_data(src, tree) -> list:
    """Extract all call site names for indexing."""
    names = []
    for node in _find_all(tree.root_node, lambda n: n.type == "call"):
        fn = node.child_by_field_name("function")
        if fn:
            if fn.type == "identifier":
                names.append(_text(fn, src).strip())
            elif fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                if attr:
                    names.append(_text(attr, src).strip())
    return names


def py_q_classes(src, tree, lines):
    return [(_r.line, _r.text) for _r in _py_q_classes_data(src, tree)]


def py_q_methods(src, tree, lines):
    return [(_r.line, _r.text) for _r in _py_q_methods_data(src, tree)]


def py_q_calls(src, tree, lines, method_name):
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "call"):
        if _py_in_literal(node):
            continue
        fn = node.child_by_field_name("function")
        if not fn:
            continue
        matched = None
        if fn.type == "identifier":
            matched = _text(fn, src).strip()
        elif fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            if attr:
                matched = _text(attr, src).strip()
        if matched != method_name:
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        raw = _text(node, src).replace("\n", " ").replace("\r", "")
        if len(raw) > 140:
            raw = raw[:140] + "…"
        results.append((_line(node), raw))
    return results


def py_q_implements(src, tree, lines, base_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        bases = _py_base_names(node, src)
        if base_name not in bases:
            continue
        name = _text(name_node, src).strip()
        results.append((_line(node), f"[class] {name}({', '.join(bases)})"))
    return results


def py_q_ident(src, tree, lines, name):
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != name:
            continue
        if _py_in_literal(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def py_q_declarations(src, tree, lines, name, include_body=False, symbol_kind=None):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("function_definition", "class_definition")):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        kind = "class" if node.type == "class_definition" else "def"
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        body_lines = "\n".join(lines[start_row:end_row + 1])
        header = f"── [{kind}] {name}  (lines {start_row + 1}–{end_row + 1}) ──"
        results.append((_line(node), f"{header}\n{body_lines}"))
    return results


def py_q_decorators(src, tree, lines, name=None):
    return [(_r.line, _r.text) for _r in _py_q_attrs_data(src, tree, name)]


def py_q_imports(src, tree, lines):
    return [(_r.line, _r.text) for _r in _py_q_imports_data(src, tree)]


def py_q_params(src, tree, lines, method_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != method_name:
            continue
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in params_node.named_children:
            if p.type == "identifier":
                param_lines.append(f"  {_text(p, src)}")
            elif p.type in ("typed_parameter", "typed_default_parameter"):
                # The name may be a plain identifier or nested inside
                # list_splat_pattern (*args: T) or dictionary_splat_pattern (**kwargs: T)
                prefix = ""
                pname = ""
                for c in p.named_children:
                    if c.type == "identifier":
                        pname = _text(c, src)
                        break
                    elif c.type == "list_splat_pattern":
                        prefix = "*"
                        pname = next((_text(gc, src) for gc in c.named_children
                                      if gc.type == "identifier"), "")
                        break
                    elif c.type == "dictionary_splat_pattern":
                        prefix = "**"
                        pname = next((_text(gc, src) for gc in c.named_children
                                      if gc.type == "identifier"), "")
                        break
                ptype = p.child_by_field_name("type")
                pt_txt = f": {_text(ptype, src)}" if ptype else ""
                dval = p.child_by_field_name("value") if p.type == "typed_default_parameter" else None
                dv_txt = f" = {_text(dval, src)}" if dval else ""
                param_lines.append(f"  {prefix}{pname}{pt_txt}{dv_txt}")
            elif p.type == "default_parameter":
                pname = next((_text(c, src) for c in p.named_children
                              if c.type == "identifier"), "")
                dval = p.child_by_field_name("value")
                dv_txt = f" = {_text(dval, src)}" if dval else ""
                param_lines.append(f"  {pname}{dv_txt}")
            elif p.type in ("list_splat_pattern", "dictionary_splat_pattern",
                            "keyword_separator", "positional_separator"):
                param_lines.append(f"  {_text(p, src)}")
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


# ── Process function ──────────────────────────────────────────────────────────

def process_py_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Parse a Python file and return list[{"line": N, "text": "..."}] for the given mode."""
    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: py_q_classes(src_bytes, tree, lines),
        "methods":      lambda: py_q_methods(src_bytes, tree, lines),
        "calls":        lambda: py_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: py_q_implements(src_bytes, tree, lines, mode_arg),
        "ident":        lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "all_refs":     lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: py_q_declarations(src_bytes, tree, lines, mode_arg),
        "decorators":   lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: py_q_imports(src_bytes, tree, lines),
        "params":       lambda: py_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if fn is None:
        raise ValueError(f"Unknown mode: {mode!r}")
    return _make_matches(fn() or [])


def describe_py_file(path: str) -> FileDescription:
    """Parse path once and return all structured Python data as a FileDescription."""
    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="py")
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="py")
    return FileDescription(
        path=path, language="py",
        classes=_py_q_classes_data(src_bytes, tree),
        methods=_py_q_methods_data(src_bytes, tree),
        imports=_py_q_imports_data(src_bytes, tree),
        attrs=_py_q_attrs_data(src_bytes, tree),
    )
