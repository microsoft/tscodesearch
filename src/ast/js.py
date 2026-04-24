"""
Shared JavaScript/TypeScript tree-sitter AST helpers.

Used by both indexserver/extractors.py (index building) and query_js.py
(per-file AST queries).  Both JS and TS grammars are supported.
"""

# ── Node type sets ─────────────────────────────────────────────────────────────

# Type declarations (TS extends JS here)
_TYPE_DECL_NODES = {
    "class_declaration",
    "interface_declaration",       # TS only
    "type_alias_declaration",      # TS only
    "enum_declaration",            # TS only
    "abstract_class_declaration",  # TS only
}

# Function / method declarations
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


# ── JS/TS-specific helpers ─────────────────────────────────────────────────────

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
    # method_definition uses 'name' as property_identifier
    for c in node.children:
        if c.type in ("identifier", "property_identifier", "string"):
            return _text(c, src).strip()
    return ""


def _fn_sig(node, src: bytes) -> str:
    """Build a readable signature for a function/method node."""
    name = _fn_name_from_node(node, src)
    params_node = node.child_by_field_name("parameters")
    ret_node = node.child_by_field_name("return_type")  # TS only
    params_txt = _text(params_node, src).strip() if params_node else "()"
    ret_txt = f": {_text(ret_node, src).strip()}" if ret_node else ""
    return f"{name}{params_txt}{ret_txt}"
