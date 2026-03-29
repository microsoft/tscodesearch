"""
Python AST query functions — extracted from query.py.

All public functions are re-exported from query.py for backward compatibility.
"""

import sys

try:
    import tree_sitter_python as tspython
    _PY_AVAILABLE = True
except ImportError:
    _PY_AVAILABLE = False

from ..ast.cs import _find_all, _text  # shared traversal helpers
from ..ast.py import (
    _PY_LITERAL_NODES,
    _line,
    _py_in_literal,
    _py_enclosing_class,
    _py_base_names,
)


def py_q_classes(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        bases = _py_base_names(node, src)
        suffix = f"({', '.join(bases)})" if bases else ""
        results.append((_line(node), f"[class] {name}{suffix}"))
    return results


def py_q_methods(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        params_str = _text(params_node, src).strip() if params_node else "()"
        ret_str = f" -> {_text(return_node, src).strip()}" if return_node else ""
        cls = _py_enclosing_class(node, src)
        kind = "method" if cls else "def"
        cls_prefix = f"[in {cls}] " if cls else ""
        results.append((_line(node), f"[{kind}] {cls_prefix}{name}{params_str}{ret_str}"))
    return results


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
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "decorator"):
        full = _text(node, src).strip()
        dname = full.lstrip("@").split("(")[0].split(".")[-1].strip()
        if name and dname != name:
            continue
        results.append((_line(node), full))
    return results


def py_q_imports(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("import_statement", "import_from_statement",
                                               "future_import_statement")):
        results.append((_line(node), _text(node, src).strip()))
    return results


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

def process_py_file(path, mode, mode_arg, show_path, count_only, context=0,
                    src_root=None, include_body=False, symbol_kind=None, uses_kind=None):
    if not _PY_AVAILABLE:
        print("ERROR: tree-sitter-python not installed. Run: pip install tree-sitter-python",
              file=sys.stderr)
        return 0

    from tree_sitter import Language, Parser
    _PY = Language(tspython.language())
    _py_parser = Parser(_PY)

    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return 0
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return 0

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: py_q_classes(src_bytes, tree, lines),
        "methods":      lambda: py_q_methods(src_bytes, tree, lines),
        "calls":        lambda: py_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: py_q_implements(src_bytes, tree, lines, mode_arg),
        "ident":        lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: py_q_declarations(src_bytes, tree, lines, mode_arg),
        "decorators":   lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: py_q_imports(src_bytes, tree, lines),
        "params":       lambda: py_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode: {mode!r}", file=sys.stderr)
        return 0

    results = fn()
    if not results:
        return 0

    import os
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
                end = min(len(lines), row + context + 1)
                for i, ln in enumerate(lines[start:end], start):
                    if i == row:
                        continue
                    prefix = f"  {_disp_base}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {ln}")
                print()
            except (ValueError, IndexError):
                pass
    return len(results)
