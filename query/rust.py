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
from tree_sitter import Language, Parser
from ._util import _make_matches, FileDescription, ClassInfo, MethodInfo

_RUST_LANG   = Language(tsrust.language())
_rust_parser = Parser(_RUST_LANG)


# ── Node type sets ─────────────────────────────────────────────────────────────

_TYPE_DECL_NODES = {
    "struct_item", "enum_item", "trait_item", "type_item",
    "union_item",
}

_FUNCTION_NODES = {
    "function_item",
}

_LITERAL_NODES = {
    "line_comment", "block_comment",
    "string_literal", "raw_string_literal",
    "char_literal",
}


# ── Basic helpers ──────────────────────────────────────────────────────────────

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


# ── Rust-specific helpers ──────────────────────────────────────────────────────

def _fn_name(node, src: bytes) -> str:
    """Get function name from a function_item node."""
    n = node.child_by_field_name("name")
    return _text(n, src).strip() if n else ""


def _type_name(node, src: bytes) -> str:
    """Get name from a type declaration node (struct/enum/trait/type)."""
    n = node.child_by_field_name("name")
    return _text(n, src).strip() if n else ""


def _impl_trait_name(node, src: bytes) -> str:
    """Get the trait name from an impl_item (None if inherent impl)."""
    t = node.child_by_field_name("trait")
    if not t:
        return ""
    # Strip generic params: "Iterator<Item=T>" → "Iterator"
    name = _text(t, src).strip()
    idx = name.find("<")
    return name[:idx].strip() if idx >= 0 else name


def _impl_type_name(node, src: bytes) -> str:
    """Get the implementing type name from an impl_item."""
    t = node.child_by_field_name("type")
    if not t:
        return ""
    name = _text(t, src).strip()
    idx = name.find("<")
    return name[:idx].strip() if idx >= 0 else name


def _fn_sig(node, src: bytes) -> str:
    """Build 'fn name(ParamType, ...) -> RetType' from a function_item."""
    name_node = node.child_by_field_name("name")
    params_node = node.child_by_field_name("parameters")
    ret_node = node.child_by_field_name("return_type")

    name = _text(name_node, src).strip() if name_node else "?"
    ret = f" -> {_text(ret_node, src).strip()}" if ret_node else ""

    if params_node:
        parts = []
        for p in params_node.named_children:
            if p.type == "parameter":
                pt = p.child_by_field_name("type")
                pn = p.child_by_field_name("pattern")
                pt_txt = _text(pt, src).strip() if pt else ""
                pn_txt = _text(pn, src).strip() if pn else ""
                parts.append(f"{pn_txt}: {pt_txt}".strip(": "))
            elif p.type == "self_parameter":
                parts.append(_text(p, src).strip())
        params_txt = ", ".join(parts)
    else:
        params_txt = ""

    return f"fn {name}({params_txt}){ret}"


# ── Data extraction functions ──────────────────────────────────────────────────

def _rust_q_classes_data(src, tree) -> list:
    """Return list[ClassInfo] for all struct/enum/trait/type declarations."""
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        name = _type_name(node, src)
        if not name:
            continue
        kind = node.type.replace("_item", "")
        results.append(ClassInfo(line=_line(node), name=name, kind=kind))
    return results


def _rust_q_methods_data(src, tree) -> list:
    """Return list[MethodInfo] for all function items and impl methods."""
    results = []
    seen = set()

    for node in _find_all(tree.root_node, lambda n: n.type == "function_item"):
        sig = _fn_sig(node, src)
        ln = _line(node)
        key = (ln, sig)
        if key not in seen:
            seen.add(key)
            p = node.parent
            in_impl = False
            impl_type = ""
            while p:
                if p.type == "impl_item":
                    in_impl = True
                    impl_type = _impl_type_name(p, src)
                    break
                p = p.parent
            kind = "method" if in_impl else "fn"
            name = _fn_name(node, src) or ""
            results.append(MethodInfo(line=ln, name=name, kind=kind,
                                           sig=sig, cls_name=impl_type))
    return results


# ── Query functions ───────────────────────────────────────────────────────────

def rust_q_classes(src, tree, lines):
    """List struct/enum/trait/type declarations."""
    return [(_r.line, _r.text) for _r in _rust_q_classes_data(src, tree)]


def rust_q_methods(src, tree, lines):
    """List function items and methods inside impl blocks."""
    return [(_r.line, _r.text) for _r in _rust_q_methods_data(src, tree)]


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

def process_rust_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Parse a Rust file and return list[{"line": N, "text": "..."}] for the given mode."""
    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = _rust_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

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
    if fn is None:
        raise ValueError(f"Unknown mode: {mode!r}")
    return _make_matches(fn() or [])


def describe_rust_file(path: str) -> FileDescription:
    """Parse path once and return all structured Rust data as a FileDescription."""
    try:
        with open(path, "rb") as _f:
            src_bytes = _f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="rust")
    try:
        tree = _rust_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="rust")
    return FileDescription(
        path=path, language="rust",
        classes=_rust_q_classes_data(src_bytes, tree),
        methods=_rust_q_methods_data(src_bytes, tree),
    )
