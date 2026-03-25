"""
Shared Python tree-sitter AST helpers.

Used by query_py.py (per-file AST queries) to keep extraction logic
consistent.  All functions operate on already-parsed tree-sitter nodes
— no parser state here.
"""

from ast_cs import _find_all, _text  # shared traversal helpers

# ── Node type sets ─────────────────────────────────────────────────────────────

_PY_LITERAL_NODES = {"comment", "string", "concatenated_string"}

# ── Basic helpers ──────────────────────────────────────────────────────────────

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
