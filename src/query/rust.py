"""
Rust AST query functions powered by tree-sitter.

Modes:
  classes      - List structs, enums, traits, type aliases
  methods      - List functions and impl methods
  calls        - Find call sites of FUNC
  implements   - Find types that impl TRAIT
  declarations - Find declaration(s) by name
  all_refs     - Find every identifier occurrence
  imports      - List use declarations
  params       - Show parameter list of FUNC
"""

EXTENSIONS = frozenset({".rs"})

import sys
import tree_sitter_rust as tsrust

from ..ast.rust import (
    _find_all, _text, _in_literal, _line,
    _TYPE_DECL_NODES, _FUNCTION_NODES, _LITERAL_NODES,
    _fn_name, _type_name, _impl_trait_name, _impl_type_name, _fn_sig,
)


# ── Query functions ───────────────────────────────────────────────────────────

def rust_q_classes(src, tree, lines):
    """List struct/enum/trait/type declarations."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        name = _type_name(node, src)
        if not name:
            continue
        kind = node.type.replace("_item", "")
        results.append((_line(node), f"[{kind}] {name}"))

    # Also list traits as "base types" of impl blocks
    return results


def rust_q_methods(src, tree, lines):
    """List function items and methods inside impl blocks."""
    results = []
    seen = set()

    # Top-level functions
    for node in _find_all(tree.root_node, lambda n: n.type == "function_item"):
        sig = _fn_sig(node, src)
        ln = _line(node)
        key = (ln, sig)
        if key not in seen:
            seen.add(key)
            # Check if inside impl
            p = node.parent
            in_impl = False
            impl_type = ""
            while p:
                if p.type == "impl_item":
                    in_impl = True
                    impl_type = _impl_type_name(p, src)
                    break
                p = p.parent
            prefix = f"[in {impl_type}] " if impl_type else ""
            kind = "method" if in_impl else "fn"
            results.append((ln, f"[{kind}] {prefix}{sig}"))
    return results


def rust_q_calls(src, tree, lines, func_name):
    """Find call sites of FUNC (bare name or Receiver::method)."""
    if "::" in func_name:
        qualifier, bare_name = func_name.rsplit("::", 1)
    else:
        qualifier, bare_name = None, func_name

    results = []
    seen_rows = set()

    # call_expression: func(args)
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
        elif fn.type == "scoped_identifier":
            # Path::func
            name_node = fn.child_by_field_name("name")
            path_node = fn.child_by_field_name("path")
            if name_node:
                matched = _text(name_node, src).strip()
                if qualifier and path_node:
                    path_txt = _text(path_node, src).strip()
                    if not (path_txt == qualifier or path_txt.endswith("::" + qualifier)):
                        matched = None
        elif fn.type == "field_expression":
            # receiver.method (chained) - treat like method call
            field = fn.child_by_field_name("field")
            if field and qualifier is None:
                matched = _text(field, src).strip()

        if matched == bare_name:
            row = node.start_point[0]
            if row not in seen_rows:
                seen_rows.add(row)
                raw = _text(node, src).replace("\n", " ")
                if len(raw) > 140:
                    raw = raw[:140] + "…"
                results.append((_line(node), raw))

    # method_call_expression: receiver.method(args)
    for node in _find_all(tree.root_node, lambda n: n.type == "method_call_expression"):
        if _in_literal(node):
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        if _text(name_node, src).strip() != bare_name:
            continue
        row = node.start_point[0]
        if row not in seen_rows:
            seen_rows.add(row)
            raw = _text(node, src).replace("\n", " ")
            if len(raw) > 140:
                raw = raw[:140] + "…"
            results.append((_line(node), raw))

    return results


def rust_q_implements(src, tree, lines, trait_name):
    """Find types that implement TRAIT."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "impl_item"):
        t = _impl_trait_name(node, src)
        if not t:
            continue
        # Match bare name ignoring generics
        bare_t = t.split("<")[0].strip().split("::")[-1]
        if bare_t != trait_name:
            continue
        impl_type = _impl_type_name(node, src)
        results.append((_line(node), f"[impl] {impl_type} : {t}"))
    return results


def rust_q_declarations(src, tree, lines, name, include_body=False):
    """Find declaration(s) named NAME."""
    results = []
    target_types = _TYPE_DECL_NODES | _FUNCTION_NODES | {"impl_item", "trait_item"}

    for node in _find_all(tree.root_node, lambda n: n.type in target_types):
        decl_name = ""
        if node.type == "function_item":
            decl_name = _fn_name(node, src)
        elif node.type in _TYPE_DECL_NODES or node.type == "trait_item":
            decl_name = _type_name(node, src)

        if decl_name != name:
            continue

        kind = node.type.replace("_item", "")
        start_row = node.start_point[0]
        end_row = node.end_point[0]

        if include_body:
            content = "\n".join(lines[start_row:end_row + 1])
        else:
            # Signature: up to opening brace
            body_node = node.child_by_field_name("body")
            if body_node:
                sig_end = body_node.start_point[0]
                content = "\n".join(lines[start_row:sig_end]).rstrip()
            else:
                content = "\n".join(lines[start_row:end_row + 1])

        header = f"── [{kind}] {name}  (lines {start_row + 1}–{end_row + 1}) ──"
        results.append((_line(node), f"{header}\n{content}"))
    return results


def rust_q_all_refs(src, tree, lines, name):
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


def rust_q_imports(src, tree, lines):
    """List use declarations."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "use_declaration"):
        results.append((_line(node), _text(node, src).strip()))
    return results


def rust_q_params(src, tree, lines, func_name):
    """Show parameter list of FUNC."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_item"):
        if _fn_name(node, src) != func_name:
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


# ── Process function ──────────────────────────────────────────────────────────

def process_rust_file(path, mode, mode_arg, show_path, count_only, context=0,
                      src_root=None, include_body=False, **kwargs):
    from tree_sitter import Language, Parser
    _RUST = Language(tsrust.language())
    _parser = Parser(_RUST)

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
        "classes":      lambda: rust_q_classes(src_bytes, tree, lines),
        "methods":      lambda: rust_q_methods(src_bytes, tree, lines),
        "calls":        lambda: rust_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: rust_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: rust_q_declarations(src_bytes, tree, lines, mode_arg,
                                                    include_body=include_body),
        "all_refs":     lambda: rust_q_all_refs(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: rust_q_imports(src_bytes, tree, lines),
        "params":       lambda: rust_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode for Rust: {mode!r}", file=sys.stderr)
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
