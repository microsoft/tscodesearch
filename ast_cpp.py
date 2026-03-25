"""
Shared C/C++ tree-sitter AST helpers.

Used by both indexserver/extractors.py (index building) and query_cpp.py
(per-file AST queries).
"""

import re

# ── Node type sets ─────────────────────────────────────────────────────────────

_TYPE_DECL_NODES = {
    "class_specifier",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
}

_FUNCTION_NODES = {
    "function_definition",
    "function_declaration",
}

_LITERAL_NODES = {
    "comment",
    "string_literal",
    "raw_string_literal",
    "char_literal",
    "concatenated_string",
}

# ── Basic helpers ──────────────────────────────────────────────────────────────

def _find_all(node, predicate, results=None):
    if results is None:
        results = []
    if predicate(node):
        results.append(node)
    for child in node.children:
        _find_all(child, predicate, results)
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


# ── C++-specific helpers ───────────────────────────────────────────────────────

def _class_name(node, src: bytes) -> str:
    """Get name from class_specifier / struct_specifier."""
    n = node.child_by_field_name("name")
    return _text(n, src).strip() if n else ""


def _base_class_names(node, src: bytes) -> list:
    """Get base class names from a class_specifier's base_class_clause."""
    names = []
    for child in node.children:
        if child.type == "base_class_clause":
            for bc in _find_all(child, lambda n: n.type in ("type_identifier", "identifier")):
                t = _text(bc, src).strip()
                if t not in ("public", "private", "protected", "virtual") and t:
                    names.append(t)
    return names


def _fn_declarator_name(node, src: bytes) -> str:
    """Recursively extract the identifier name from a declarator node."""
    # function_declarator → declarator → (pointer_declarator →)* identifier / qualified_identifier
    if node.type in ("identifier", "field_identifier"):
        return _text(node, src).strip()
    if node.type == "qualified_identifier":
        # A::B::foo → just return the last part
        scope = node.child_by_field_name("scope")
        name_node = node.child_by_field_name("name")
        if name_node:
            return _text(name_node, src).strip()
        return _text(node, src).strip()
    # recurse into declarator / pointer_declarator
    decl = node.child_by_field_name("declarator")
    if decl:
        return _fn_declarator_name(decl, src)
    for c in node.children:
        r = _fn_declarator_name(c, src)
        if r:
            return r
    return ""


def _fn_name(node, src: bytes) -> str:
    """Get function name from a function_definition node."""
    decl = node.child_by_field_name("declarator")
    if not decl:
        return ""
    # declarator might be function_declarator directly
    if decl.type == "function_declarator":
        inner = decl.child_by_field_name("declarator")
        return _fn_declarator_name(inner, src) if inner else ""
    return _fn_declarator_name(decl, src)


def _fn_sig(node, src: bytes) -> str:
    """Build a signature string for a function_definition node."""
    name = _fn_name(node, src)
    # Return type is the type child
    type_node = node.child_by_field_name("type")
    ret_txt = _text(type_node, src).strip() if type_node else ""
    # Parameter list
    decl = node.child_by_field_name("declarator")
    params_node = None
    if decl:
        if decl.type == "function_declarator":
            params_node = decl.child_by_field_name("parameters")
        else:
            # could be pointer_declarator wrapping function_declarator
            for c in _find_all(decl, lambda n: n.type == "function_declarator"):
                params_node = c.child_by_field_name("parameters")
                break
    params_txt = _text(params_node, src).strip() if params_node else "()"
    return f"{ret_txt} {name}{params_txt}".strip()
