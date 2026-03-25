"""
Shared C# tree-sitter AST helpers.

Used by both indexer.py (pre-filter index building) and query.py (per-file AST queries)
to keep extraction logic consistent and avoid drift between the two.

All functions operate on already-parsed tree-sitter nodes — no parser state here.
"""

import re

# ── Node type sets ─────────────────────────────────────────────────────────────

_TYPE_DECL_NODES = {
    "class_declaration", "interface_declaration", "struct_declaration",
    "enum_declaration", "record_declaration", "delegate_declaration",
}

_MEMBER_DECL_NODES = {
    "method_declaration", "constructor_declaration", "property_declaration",
    "field_declaration", "event_declaration", "event_field_declaration",
    "local_function_statement",
}

# Map user-facing symbol_kind strings to the tree-sitter node types they cover.
# "type" and "member" are umbrella aliases for all type / all member declarations.
SYMBOL_KIND_TO_NODES: dict[str, frozenset] = {
    "method":      frozenset({"method_declaration", "local_function_statement"}),
    "constructor": frozenset({"constructor_declaration"}),
    "property":    frozenset({"property_declaration"}),
    "field":       frozenset({"field_declaration"}),
    "event":       frozenset({"event_declaration", "event_field_declaration"}),
    "class":       frozenset({"class_declaration"}),
    "interface":   frozenset({"interface_declaration"}),
    "struct":      frozenset({"struct_declaration"}),
    "enum":        frozenset({"enum_declaration"}),
    "record":      frozenset({"record_declaration"}),
    "delegate":    frozenset({"delegate_declaration"}),
    "type":        frozenset(_TYPE_DECL_NODES),
    "member":      frozenset(_MEMBER_DECL_NODES),
}

# Typesense field to search when narrowing by symbol_kind.
_TYPE_KINDS   = frozenset({"class", "interface", "struct", "enum", "record", "delegate", "type"})
_MEMBER_KINDS = frozenset({"method", "constructor", "property", "field", "event", "member"})

def symbol_kind_query_by(kind: str) -> str:
    """Return the Typesense query_by string for a given symbol_kind.

    Returns empty string when kind is unknown (caller should fall back to default).
    """
    k = kind.lower().strip() if kind else ""
    if k in _TYPE_KINDS:
        return "class_names,filename"
    if k in _MEMBER_KINDS:
        return "method_names,filename"
    return ""

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


_QUALIFIED_RE = re.compile(r'(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)')


def _unqualify(name: str) -> str:
    """Strip namespace prefix: 'A.B.IFoo' → 'IFoo'."""
    return name.rsplit(".", 1)[-1]


def _unqualify_type(text: str) -> str:
    """Strip namespace prefixes from all qualified names in a type string.

    'Task<Acme.Widget>'                      → 'Task<Widget>'
    'System.Collections.Generic.List<int>'   → 'List<int>'
    """
    return _QUALIFIED_RE.sub(r'\1', text)


# ── Semantic helpers ───────────────────────────────────────────────────────────

def _base_type_names(node, src: bytes) -> list:
    """Extract all type names from the base_list of a type declaration.

    In tree-sitter-c-sharp 0.23.x, base_list is a direct child (no named field),
    and its contents are identifier/generic_name/qualified_name nodes — NOT wrapped
    in simple_base_type as in earlier grammar versions.
    """
    names = []
    base_list = next((c for c in node.children if c.type == "base_list"), None)
    if not base_list:
        return names
    for child in base_list.children:
        if not child.is_named:
            continue  # skip punctuation (: and ,)
        if child.type == "identifier":
            names.append(_text(child, src).strip())
        elif child.type == "generic_name":
            # IFoo<T> — first named child is the bare identifier
            if child.named_children:
                names.append(_text(child.named_children[0], src).strip())
        elif child.type == "qualified_name":
            # Keep full qualified text so callers can choose to unqualify
            names.append(_text(child, src).strip())
        elif child.type in ("simple_base_type", "primary_constructor_base_type"):
            # Older grammar versions wrapped types in simple_base_type
            t = child.child_by_field_name("type") or child.child_by_field_name("name")
            if t:
                names.append(_text(t, src).strip())
            elif child.named_children:
                names.append(_text(child.named_children[0], src).strip())
    return names


def _collect_ctor_names(root, src: bytes) -> list:
    """Return the type name from every 'new Foo(...)' expression in the AST.

    Used by the indexer to populate call_sites, and by q_calls to find
    constructor call sites.  Returns bare (rightmost) identifier only,
    e.g. 'new A.B.Foo(...)' → 'Foo'.
    """
    names = []
    for node in _find_all(root, lambda n: n.type == "object_creation_expression"):
        type_node = node.child_by_field_name("type")
        if type_node:
            idents = _find_all(type_node, lambda n: n.type == "identifier")
            if idents:
                names.append(_text(idents[-1], src))
    return names
