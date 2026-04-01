"""
C/C++ AST query functions powered by tree-sitter.

Modes:
  classes      - List class/struct/union/enum declarations
  methods      - List function definitions
  calls        - Find call sites of FUNC
  implements   - Find classes that inherit from BASE
  declarations - Find declaration(s) by name
  all_refs     - Find every identifier occurrence
  includes     - List #include directives
  params       - Show parameter list of FUNC
"""

EXTENSIONS = frozenset({".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"})

import sys
import tree_sitter_cpp as tscpp

from ..ast.cpp import (
    _find_all, _text, _in_literal, _line,
    _TYPE_DECL_NODES, _FUNCTION_NODES, _LITERAL_NODES,
    _class_name, _base_class_names, _fn_name, _fn_sig,
)


# ── Query functions ───────────────────────────────────────────────────────────

def cpp_q_classes(src, tree, lines):
    """List class/struct/union/enum declarations."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        name = _class_name(node, src)
        if not name:
            continue
        kind = node.type.replace("_specifier", "").replace("_", " ")
        bases = _base_class_names(node, src)
        suffix = f" : {', '.join(bases)}" if bases else ""
        results.append((_line(node), f"[{kind}] {name}{suffix}"))
    return results


def cpp_q_methods(src, tree, lines):
    """List function definitions."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name = _fn_name(node, src)
        if not name:
            continue
        sig = _fn_sig(node, src)
        # Determine enclosing class (for methods)
        p = node.parent
        cls_name = ""
        while p:
            if p.type in _TYPE_DECL_NODES:
                cls_name = _class_name(p, src)
                break
            p = p.parent
        prefix = f"[in {cls_name}] " if cls_name else ""
        kind = "method" if cls_name else "function"
        results.append((_line(node), f"[{kind}] {prefix}{sig}"))
    return results


def cpp_q_calls(src, tree, lines, func_name):
    """Find call sites of FUNC."""
    # Support qualified name: Class::method or obj.method
    if "::" in func_name:
        qualifier, bare_name = func_name.rsplit("::", 1)
        qual_sep = "::"
    elif "." in func_name:
        qualifier, bare_name = func_name.rsplit(".", 1)
        qual_sep = "."
    else:
        qualifier, bare_name = None, func_name
        qual_sep = None

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
        elif fn.type == "field_expression":
            field = fn.child_by_field_name("field")
            arg   = fn.child_by_field_name("argument")
            if field:
                matched = _text(field, src).strip()
                if qualifier and arg:
                    arg_txt = _text(arg, src).strip()
                    if not (arg_txt == qualifier or arg_txt.endswith(qual_sep + qualifier)):
                        matched = None
        elif fn.type == "qualified_identifier":
            name_node = fn.child_by_field_name("name")
            scope_node = fn.child_by_field_name("scope")
            if name_node:
                matched = _text(name_node, src).strip()
                if qualifier and scope_node:
                    scope_txt = _text(scope_node, src).strip()
                    if not (scope_txt == qualifier or scope_txt.endswith("::" + qualifier)):
                        matched = None

        if matched == bare_name:
            row = node.start_point[0]
            if row not in seen_rows:
                seen_rows.add(row)
                raw = _text(node, src).replace("\n", " ")
                if len(raw) > 140:
                    raw = raw[:140] + "…"
                results.append((_line(node), raw))

    return results


def cpp_q_implements(src, tree, lines, base_name):
    """Find classes/structs that inherit from BASE."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        bases = _base_class_names(node, src)
        if base_name not in bases:
            continue
        name = _class_name(node, src)
        if not name:
            continue
        kind = node.type.replace("_specifier", "")
        suffix = ", ".join(bases)
        results.append((_line(node), f"[{kind}] {name} : {suffix}"))
    return results


def cpp_q_declarations(src, tree, lines, name, include_body=False):
    """Find declaration(s) named NAME."""
    results = []
    target_types = _TYPE_DECL_NODES | _FUNCTION_NODES

    for node in _find_all(tree.root_node, lambda n: n.type in target_types):
        if node.type in _TYPE_DECL_NODES:
            decl_name = _class_name(node, src)
        else:
            decl_name = _fn_name(node, src)

        if not decl_name or decl_name != name:
            continue

        kind = node.type.replace("_specifier", "").replace("_definition", "").replace("_declaration", "")
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


def cpp_q_all_refs(src, tree, lines, name):
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


def cpp_q_includes(src, tree, lines):
    """List #include directives."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "preproc_include"):
        results.append((_line(node), _text(node, src).strip()))
    return results


def cpp_q_params(src, tree, lines, func_name):
    """Show parameter list of FUNC."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        if _fn_name(node, src) != func_name:
            continue
        # Find the function_declarator to get its parameters
        decl = node.child_by_field_name("declarator")
        params_node = None
        if decl:
            if decl.type == "function_declarator":
                params_node = decl.child_by_field_name("parameters")
            else:
                for c in _find_all(decl, lambda n: n.type == "function_declarator"):
                    params_node = c.child_by_field_name("parameters")
                    break
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in params_node.named_children:
            if p.type == "parameter_declaration":
                param_lines.append(f"  {_text(p, src).strip()}")
            elif p.type == "variadic_parameter_declaration":
                param_lines.append(f"  {_text(p, src).strip()}")
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


# ── Process function ──────────────────────────────────────────────────────────

def process_cpp_file(path, mode, mode_arg, show_path, count_only, context=0,
                     src_root=None, include_body=False, **kwargs):
    from tree_sitter import Language, Parser
    _CPP = Language(tscpp.language())
    _parser = Parser(_CPP)

    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return 0
    try:
        tree = _parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return 0

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: cpp_q_classes(src_bytes, tree, lines),
        "methods":      lambda: cpp_q_methods(src_bytes, tree, lines),
        "calls":        lambda: cpp_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: cpp_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: cpp_q_declarations(src_bytes, tree, lines, mode_arg,
                                                   include_body=include_body),
        "all_refs":     lambda: cpp_q_all_refs(src_bytes, tree, lines, mode_arg),
        "includes":     lambda: cpp_q_includes(src_bytes, tree, lines),
        "params":       lambda: cpp_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode for C/C++: {mode!r}", file=sys.stderr)
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
