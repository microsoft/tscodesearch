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
    """Get base class names from a class_specifier's base_class_clause.

    Iterates direct children only — does NOT recurse — so template type
    arguments (e.g. the T in Base<T>) are never mistaken for base classes.
    Handles simple types (Foo), qualified types (A::Foo), and template
    types (Foo<T>).
    """
    names = []
    for child in node.children:
        if child.type != "base_class_clause":
            continue
        for item in child.children:
            if item.type == "type_identifier":
                t = _text(item, src).strip()
                if t not in ("public", "private", "protected", "virtual") and t:
                    names.append(t)
            elif item.type == "qualified_identifier":
                # A::B::Foo → Foo  /  A::B::Foo<T> → Foo
                name_node = item.child_by_field_name("name")
                if name_node:
                    if name_node.type == "template_type":
                        tname = name_node.child_by_field_name("name")
                        if tname:
                            names.append(_text(tname, src).strip())
                    else:
                        names.append(_text(name_node, src).strip())
            elif item.type == "template_type":
                # Foo<T> → Foo (ignore template args)
                name_node = item.child_by_field_name("name")
                if name_node:
                    names.append(_text(name_node, src).strip())
    return names


def _member_fn_name(node, src: bytes) -> str:
    """Return the function name if node is a field_declaration for a member function.

    Handles pure virtual (virtual void init() = 0), const, and pointer-return
    variants.  Returns "" for ordinary field declarations (int x, etc.).
    """
    if node.type != "field_declaration":
        return ""
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return ""
    if decl.type == "function_declarator":
        inner = decl.child_by_field_name("declarator")
        return _fn_declarator_name(inner, src) if inner else ""
    if decl.type in ("pointer_declarator", "reference_declarator"):
        inner = decl.child_by_field_name("declarator")
        if inner and inner.type == "function_declarator":
            name_node = inner.child_by_field_name("declarator")
            return _fn_declarator_name(name_node, src) if name_node else ""
    return ""


def _member_fn_sig(node, src: bytes) -> str:
    """Build a signature string for a field_declaration member function."""
    name = _member_fn_name(node, src)
    if not name:
        return ""
    type_node = node.child_by_field_name("type")
    ret_txt = _text(type_node, src).strip() if type_node else ""
    decl = node.child_by_field_name("declarator")
    params_node = None
    if decl:
        fn_decl = decl if decl.type == "function_declarator" else None
        if fn_decl is None and decl.type in ("pointer_declarator", "reference_declarator"):
            fn_decl = decl.child_by_field_name("declarator")
        if fn_decl and fn_decl.type == "function_declarator":
            params_node = fn_decl.child_by_field_name("parameters")
    params_txt = _text(params_node, src).strip() if params_node else "()"
    return f"{ret_txt} {name}{params_txt}".strip()


def _fn_declarator_name(node, src: bytes) -> str:
    """Recursively extract the identifier name from a declarator node."""
    # function_declarator → declarator → (pointer_declarator →)* identifier / qualified_identifier
    if node.type in ("identifier", "field_identifier"):
        return _text(node, src).strip()
    if node.type == "qualified_identifier":
        # A::B::foo → just return the last part (may itself be operator_name)
        name_node = node.child_by_field_name("name")
        if name_node:
            return _fn_declarator_name(name_node, src)
        return _text(node, src).strip()
    if node.type == "operator_name":
        # operator+, operator[], operator=, etc. — return the full token
        return _text(node, src).strip()
    if node.type == "destructor_name":
        # ~ClassName — return as-is so it's distinguishable from the constructor
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
