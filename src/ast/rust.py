"""
Shared Rust tree-sitter AST helpers.

Used by both indexserver/extractors.py (index building) and query_rust.py
(per-file AST queries).
"""

import re

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
