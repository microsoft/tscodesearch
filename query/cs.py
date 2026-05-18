"""
C# AST query functions.

Tree-sitter walkers for every C# query mode used by ``query.dispatch.query_file``
and ``indexserver.indexer.extract_metadata`` (which feeds the index fields).
The package's public re-exports live in ``query/__init__.py``.
"""

EXTENSIONS = frozenset({".cs"})

import re
import sys
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser
from ._util import _run_dispatch, FileDescription, ClassInfo, MethodInfo, FieldInfo, ImportInfo, AttrInfo, CallSiteInfo, CastInfo, LocalVarInfo, MemberAccessInfo, TreeIndex

_CS_LANG = Language(tscsharp.language())
_cs_parser = Parser(_CS_LANG)


# -- C# preprocessor normaliser ------------------------------------------------

_PP_RE = re.compile(rb'^\s*#\s*(\w+)')

def _strip_else_branches(src_bytes: bytes) -> bytes:
    """Pre-process C# source: assume all #if conditions are true, blank #else/#elif branches."""
    lines = src_bytes.splitlines(keepends=True)
    result: list[bytes] = []
    skip_stack: list[bool] = []

    def _skipping() -> bool:
        return bool(skip_stack) and skip_stack[-1]

    for line in lines:
        m = _PP_RE.match(line)
        directive = m.group(1).lower() if m else b""
        if directive in (b"if", b"ifdef", b"ifndef"):
            skip_stack.append(_skipping())
            result.append(b"\n")
        elif directive in (b"elif", b"else"):
            if skip_stack:
                skip_stack[-1] = True
            result.append(b"\n")
        elif directive == b"endif":
            if skip_stack:
                skip_stack.pop()
            result.append(b"\n")
        elif directive:
            result.append(b"\n")
        else:
            result.append(b"\n" if _skipping() else line)
    return b"".join(result)

# -- Inlined from src/ast/cs.py ----------------------------------------------

_TYPE_DECL_NODES = {
    "class_declaration", "interface_declaration", "struct_declaration",
    "enum_declaration", "record_declaration", "delegate_declaration",
}

_MEMBER_DECL_NODES = {
    "method_declaration", "constructor_declaration", "property_declaration",
    "field_declaration", "event_declaration", "event_field_declaration",
    "local_function_statement",
}

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

_TYPE_KINDS   = frozenset({"class", "interface", "struct", "enum", "record", "delegate", "type"})
_MEMBER_KINDS = frozenset({"method", "constructor", "property", "field", "event", "member"})

# Union of every node type bucketed by describe_cs_file's extractors. Passed
# to _CsIndex when describing a file so a single tree walk feeds all of them.
_DESCRIBE_NODE_TYPES: frozenset = frozenset(
    _TYPE_DECL_NODES
    | _MEMBER_DECL_NODES
    | {
        "namespace_declaration", "file_scoped_namespace_declaration",
        "using_directive", "attribute",
        "invocation_expression", "object_creation_expression",
        "member_access_expression", "cast_expression",
        "local_declaration_statement",
    }
)


def symbol_kind_query_by(kind: str) -> str:
    """Return the search query_by string for a given symbol_kind.

    Returns empty string when kind is unknown (caller should fall back to default).
    """
    k = kind.lower().strip() if kind else ""
    if k in _TYPE_KINDS:
        return "class_names,filename"
    if k in _MEMBER_KINDS:
        return "method_names,filename"
    return ""


def _find_all(node, predicate, results=None):
    if results is None:
        results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if predicate(n):
            results.append(n)
        # Reverse so leftmost children are processed first
        stack.extend(reversed(n.children))
    return results


def _CsIndex(src: bytes, tree, wanted, collect_refs: bool = False) -> TreeIndex:
    """C#-flavoured TreeIndex: feeds the right literal/identifier types."""
    return TreeIndex(
        src, tree, wanted,
        literal_nodes=_LITERAL_NODES if collect_refs else None,
        identifier_types=("identifier",) if collect_refs else (),
    )


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_QUALIFIED_RE = re.compile(r'(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)')


def _unqualify(name: str) -> str:
    """Strip namespace prefix: 'A.B.IFoo' -> 'IFoo'."""
    return name.rsplit(".", 1)[-1]


def _unqualify_type(text: str) -> str:
    """Strip namespace prefixes from all qualified names in a type string."""
    return _QUALIFIED_RE.sub(r'\1', text)


def _base_type_names(node, src: bytes) -> list:
    """Extract all type names from the base_list of a type declaration."""
    names = []
    base_list = next((c for c in node.children if c.type == "base_list"), None)
    if not base_list:
        return names
    for child in base_list.children:
        if not child.is_named:
            continue
        if child.type == "identifier":
            names.append(_text(child, src).strip())
        elif child.type == "generic_name":
            if child.named_children:
                names.append(_text(child.named_children[0], src).strip())
        elif child.type == "qualified_name":
            names.append(_text(child, src).strip())
        elif child.type in ("simple_base_type", "primary_constructor_base_type"):
            t = child.child_by_field_name("type") or child.child_by_field_name("name")
            if t:
                names.append(_text(t, src).strip())
            elif child.named_children:
                names.append(_text(child.named_children[0], src).strip())
    return names


def _collect_ctor_names(idx: TreeIndex, src: bytes) -> list:
    """Return the type name from every 'new Foo(...)' expression in the AST."""
    names = []
    for node in idx.of("object_creation_expression"):
        type_node = node.child_by_field_name("type")
        if type_node:
            idents = _find_all(type_node, lambda n: n.type == "identifier")
            if idents:
                names.append(_text(idents[-1], src))
    return names

# -- AST helpers ---------------------------------------------------------------

def _line(node) -> int:
    return node.start_point[0] + 1


def _end_line(node) -> int:
    """1-indexed inclusive last line of ``node``'s extent."""
    return node.end_point[0] + 1


def _strip_generic(name: str) -> str:
    idx = name.find("<")
    return name[:idx].strip() if idx >= 0 else name.strip()


def _type_names(type_txt: str) -> set:
    return set(re.findall(r'[A-Za-z_]\w*', _unqualify_type(type_txt)))


def _truncate_raw(node, src, limit: int = 140) -> str:
    """Return the node's text as a single line, truncated to `limit` chars."""
    raw = _text(node, src).replace("\n", " ").replace("\r", "")
    return raw[:limit] + "..." if len(raw) > limit else raw


def _node_kind(node) -> str:
    """Human-readable kind label derived from the node type name."""
    return (node.type
            .replace("_declaration", "")
            .replace("statement", "")
            .replace("_", " ")
            .strip())


def _cls_prefix(node, src) -> str:
    """Return '[in ClassName] ' if node is inside a named type, else ''."""
    cls = _enclosing_type_name(node, src)
    return f"[in {cls}] " if cls else ""


def _enclosing_member_name(node, src) -> str:
    """Walk up to find the enclosing member declaration and return its name.

    Returns ``""`` when the node is at type-level (e.g. a static initialiser
    sitting outside any member) or at namespace level. For ``field_declaration``
    / ``event_field_declaration`` (which carry their name inside a nested
    ``variable_declarator`` rather than a direct ``name`` field), the first
    declarator's name is returned as the member identifier.
    """
    p = node.parent
    while p is not None:
        if p.type in _MEMBER_DECL_NODES:
            name_node = p.child_by_field_name("name")
            if name_node is None and p.type in (
                    "field_declaration", "event_field_declaration"):
                vd = next((c for c in p.children
                           if c.type == "variable_declaration"), None)
                if vd is not None:
                    decl = next((c for c in vd.children
                                 if c.type == "variable_declarator"), None)
                    if decl is not None:
                        name_node = decl.child_by_field_name("name")
            return _text(name_node, src).strip() if name_node is not None else ""
        p = p.parent
    return ""


def _scope_prefix(node, src) -> str:
    """Return ``'[in TypeName.MemberName] '`` for a node inside a member,
    ``'[in TypeName] '`` for a node at type-level, or ``''`` at namespace
    level. Used to enrich AST hits in pattern modes (``calls`` / ``uses`` /
    ``accesses_of`` / ``accesses_on`` / ``casts``) with the enclosing scope
    so the agent doesn't have to issue a follow-up ``at LINE:COL`` query.
    """
    type_name   = _enclosing_type_name(node, src)
    member_name = _enclosing_member_name(node, src)
    if type_name and member_name:
        return f"[in {type_name}.{member_name}] "
    if type_name:
        return f"[in {type_name}] "
    return ""


def _passes_enclosing_filter(node, src,
                             enclosing_method: str | None,
                             enclosing_class: str | None) -> bool:
    """True if ``node`` lives inside a member named ``enclosing_method`` and
    a type named ``enclosing_class``. Either filter can be ``None`` / empty
    to skip that dimension. Used to narrow pattern-mode hits to a specific
    call-site context, e.g. ``calls("Save", enclosing_method="WriteBack")``.
    """
    if enclosing_method:
        if _enclosing_member_name(node, src) != enclosing_method:
            return False
    if enclosing_class:
        if _enclosing_type_name(node, src) != enclosing_class:
            return False
    return True


_LITERAL_NODES = {
    "comment", "string_literal", "verbatim_string_literal",
    "interpolated_string_expression", "character_literal",
    "interpolated_verbatim_string_expression",
}


def _in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _LITERAL_NODES:
            return True
        p = p.parent
    return False


def _field_type(node, src) -> str:
    for child in node.children:
        if child.type == "variable_declaration":
            t = child.child_by_field_name("type")
            if t:
                return _text(t, src).strip()
    return ""


def _sig_tokens(node, src) -> list:
    """Every ``identifier``-typed token inside a member declaration, except
    those inside the method body.

    Captures the bits of a signature that aren't already stored in dedicated
    structured fields -- attribute names, parameter names, default-value
    identifiers, generic args, constraint targets, etc. Modifiers like
    ``public`` / ``async`` are tree-sitter keywords (not ``identifier``
    nodes), so they're naturally excluded.
    """
    body = node.child_by_field_name("body")
    out: list = []
    seen: set = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n is body:
            continue
        if n.type == "identifier":
            t = _text(n, src)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
            continue
        for c in n.children:
            stack.append(c)
    return out


def _build_sig(node, src) -> str:
    ret   = node.child_by_field_name("returns") or node.child_by_field_name("type")
    name  = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")
    if not name:
        return ""
    ret_txt  = _text(ret, src).strip() if ret else ""
    name_txt = _text(name, src).strip()
    if params:
        parts = []
        for p in _find_all(params, lambda n: n.type == "parameter"):
            pt = p.child_by_field_name("type")
            pn = p.child_by_field_name("name")
            pt_txt = _text(pt, src).strip() if pt else ""
            pn_txt = _text(pn, src).strip() if pn else ""
            parts.append(f"{pt_txt} {pn_txt}".strip())
        params_txt = ", ".join(parts)
    else:
        params_txt = ""
    return f"{ret_txt} {name_txt}({params_txt})".strip() if ret_txt else f"{name_txt}({params_txt})"


_CS_VISIBILITY_TOKENS = ("public", "internal", "protected", "private")


def _cs_explicit_visibility(node) -> str:
    """Return the most-specific access modifier present on ``node`` as a
    canonical single-word string, or ``""`` if none is written.

    C# allows compound modifiers (``protected internal``, ``private
    protected``). For filtering purposes the *outermost* visibility is the
    one that matters: ``protected internal`` is reachable from outside the
    assembly through inheritance, so it's classified as ``protected``;
    ``private protected`` is the most-restricted form, classified as
    ``private``. Anything else collapses to the matching keyword.
    """
    mods = [c for c in node.children if c.type == "modifier"]
    if not mods:
        return ""
    seen = []
    for m in mods:
        for child in m.children:
            t = child.type
            if t in _CS_VISIBILITY_TOKENS and t not in seen:
                seen.append(t)
    if not seen:
        return ""
    if "private" in seen and "protected" in seen:
        return "private"     # private protected = most restricted
    if "protected" in seen and "internal" in seen:
        return "protected"   # protected internal -- reachable via inheritance
    # Single-keyword forms.
    for tok in _CS_VISIBILITY_TOKENS:
        if tok in seen:
            return tok
    return ""


def _cs_type_visibility(node) -> str:
    """Visibility for a top-level type declaration. Explicit modifier wins;
    otherwise nested types in a class default to ``private``, and types at
    namespace/file level default to ``internal`` (C# defaults)."""
    explicit = _cs_explicit_visibility(node)
    if explicit:
        return explicit
    # Walk up to find the immediate scope: another type -> private; else
    # namespace / compilation_unit -> internal.
    p = node.parent
    while p is not None:
        if p.type in _TYPE_DECL_NODES:
            return "private"
        if p.type in ("namespace_declaration",
                      "file_scoped_namespace_declaration",
                      "compilation_unit"):
            return "internal"
        p = p.parent
    return "internal"


def _cs_member_visibility(node) -> str:
    """Visibility for a member declaration (method, field, prop, event,
    ctor). Explicit modifier wins; absence of any modifier inside an
    interface or enum defaults to ``public`` (the language's rule).
    Anywhere else, absence defaults to ``private``."""
    explicit = _cs_explicit_visibility(node)
    if explicit:
        return explicit
    p = node.parent
    while p is not None:
        if p.type == "interface_declaration":
            return "public"
        if p.type == "enum_declaration":
            return "public"
        if p.type in _TYPE_DECL_NODES:
            return "private"
        p = p.parent
    return "private"


def _enclosing_type_name(node, src) -> str:
    p = node.parent
    while p:
        if p.type in _TYPE_DECL_NODES:
            nn = p.child_by_field_name("name")
            if nn:
                return _text(nn, src).strip()
        p = p.parent
    return ""


# -- Block-scoped variable-type resolver ---------------------------------------

# Node types that introduce a new variable scope. A call site inside one of
# these nodes resolves variable names against this node's local map first,
# then walks outward through every enclosing scope. The grammar's ``block``
# nodes are included so sibling blocks (if/else branches, try and each catch,
# the two arms of a switch) isolate their declarations -- without that, real
# code like ``if (b) { Foo x = ...; } else { Bar x = ...; }`` collides under
# a single method-wide map.
_SCOPE_NODES = frozenset({
    # Method-like containers (carry parameters but no body locals -- those
    # live in the body block, which is its own scope below).
    "method_declaration", "constructor_declaration", "destructor_declaration",
    "operator_declaration", "conversion_operator_declaration",
    "local_function_statement",
    "accessor_declaration",
    "lambda_expression", "anonymous_method_expression",
    # Block and the statements that declare a variable visible only inside
    # their body.
    "block",
    "catch_clause",
    "for_statement",
    "foreach_statement",
    "using_statement",
})

# Subset of scope nodes that carry a ``parameters`` field -- used to decide
# which declaration channels the per-scope walker should consult.
_PARAMETERIZED_SCOPES = frozenset({
    "method_declaration", "constructor_declaration", "destructor_declaration",
    "operator_declaration", "conversion_operator_declaration",
    "local_function_statement",
    "accessor_declaration",
    "lambda_expression", "anonymous_method_expression",
})


class _VarTypeMap:
    """Method-scoped variable name -> resolved type.

    ``resolve_at(name, node)`` walks up from ``node`` through every enclosing
    scope node, returning the first matching declared/inferred type. A name
    that maps to conflicting types within one scope is sentinelled to ``None``
    and never produces a qualified call form.

    Scope identity is keyed by ``tree_sitter.Node.id`` (the underlying C-side
    node pointer) rather than Python ``id()``, because tree-sitter creates
    fresh Python wrapper objects for each traversal -- ``node.parent`` from
    one call site produces a different Python object than the same node
    found via a top-down walk, even though both wrap the same AST node.
    """

    __slots__ = ("_scope_maps", "_file_map")

    def __init__(self, scope_maps: dict[int, dict[str, str | None]],
                 file_map: dict[str, str | None]):
        self._scope_maps = scope_maps
        self._file_map = file_map

    def resolve_at(self, var_name: str, node) -> str | None:
        """Return resolved type for ``var_name`` from ``node``'s position.

        Returns ``None`` when the name is unknown or conflicts in its scope.
        """
        if not var_name:
            return None
        p = node
        while p is not None:
            sm = self._scope_maps.get(p.id)
            if sm is not None and var_name in sm:
                return sm[var_name]
            p = p.parent
        return self._file_map.get(var_name)


_MISSING = object()


def _scope_add(m: dict, name: str, type_txt: str) -> None:
    """Insert name->type into m, suppressing on conflict (set to None)."""
    if not name or not type_txt:
        return
    existing = m.get(name, _MISSING)
    if existing is _MISSING:
        m[name] = type_txt
    elif existing is None:
        return
    elif existing != type_txt:
        m[name] = None


def _generic_type_arg(generic_node, src) -> str:
    """Return the first type argument from a ``generic_name``'s
    ``type_argument_list`` child, or ``""`` if not present."""
    for c in generic_node.children:
        if c.type == "type_argument_list":
            for g in c.children:
                if g.is_named:
                    return _text(g, src).strip()
    return ""


def _infer_var_type(expr, src, scope_map: dict) -> str:
    """Best-effort syntactic type inference for a ``var`` initialiser.

    Handles, in order:
      * ``await E``                            -- unwrap and recurse
      * ``new T(...)`` / ``new T[...]``        -- exact type
      * ``(T)expr`` / ``expr as T``            -- cast/as target
      * ``arr[i]`` where ``arr: T[]``          -- element type from scope
      * ``GenericMethod<T>(...)``              -- first type arg (DI/factory idiom)
      * ``recv.GenericMethod<T>(...)``         -- same; ignores the receiver
      * ``TypeName.Method(...)``               -- assume TypeName is the result
                                                 (static factory idiom)

    The factory and generic heuristics deliberately favour over-emission:
    AI agents post-filter results, and a missing qualified form is worse
    than a few harmless extras (they don't match a real call line in the
    AST stage anyway).

    Returns ``""`` when no plausible type is derivable.
    """
    if expr is None:
        return ""

    # Strip await wrappers -- ``await E`` has the same observed type as E for
    # our purposes (we don't model Task<T> unwrapping, but the inner type
    # is almost always more useful than nothing).
    if expr.type == "await_expression":
        inner = next((c for c in expr.children if c.is_named), None)
        return _infer_var_type(inner, src, scope_map)

    t = expr.type
    if t == "object_creation_expression":
        tn = expr.child_by_field_name("type")
        return _text(tn, src).strip() if tn else ""
    if t == "array_creation_expression":
        tn = expr.child_by_field_name("type")
        return _text(tn, src).strip() if tn else ""
    if t == "cast_expression":
        tn = expr.child_by_field_name("type")
        return _text(tn, src).strip() if tn else ""
    if t == "as_expression":
        tn = expr.child_by_field_name("right") or expr.child_by_field_name("type")
        return _text(tn, src).strip() if tn else ""
    if t == "element_access_expression":
        obj = expr.child_by_field_name("expression")
        if obj and obj.type == "identifier":
            arr_type = scope_map.get(_text(obj, src).strip())
            if isinstance(arr_type, str) and arr_type.endswith("[]"):
                return arr_type[:-2].strip()
        return ""
    if t == "invocation_expression":
        fn = expr.child_by_field_name("function")
        if fn is None:
            return ""
        # Bare ``GenericMethod<T>(...)`` -- the type argument is almost
        # always the return type (DI ``Resolve<T>``, ``Get<T>`` patterns).
        if fn.type == "generic_name":
            ta = _generic_type_arg(fn, src)
            if ta:
                return ta
        if fn.type == "member_access_expression":
            name     = fn.child_by_field_name("name")
            receiver = fn.child_by_field_name("expression")
            # ``recv.GenericMethod<T>(...)`` -- same heuristic.
            if name is not None and name.type == "generic_name":
                ta = _generic_type_arg(name, src)
                if ta:
                    return ta
            # ``TypeName.Method(...)`` -- static factory pattern. Receiver is
            # a bare PascalCase identifier and isn't a declared variable in
            # this scope. False-positive friendly: a static method that
            # returns something other than its enclosing type still yields
            # a qualified form that AST post-filtering will reject if no
            # matching call exists.
            if (receiver is not None
                    and receiver.type == "identifier"):
                rtxt = _text(receiver, src).strip()
                if (rtxt and rtxt[:1].isupper()
                        and scope_map.get(rtxt, _MISSING) is _MISSING):
                    return rtxt
        return ""

    if t == "member_access_expression":
        name     = expr.child_by_field_name("name")
        receiver = expr.child_by_field_name("expression")
        if name is None:
            return ""
        # Static property access: ``TypeName.Member`` -- guess TypeName.
        # Mirrors the invocation-side factory heuristic.
        if (receiver is not None and receiver.type == "identifier"):
            rtxt = _text(receiver, src).strip()
            if (rtxt and rtxt[:1].isupper()
                    and scope_map.get(rtxt, _MISSING) is _MISSING):
                return rtxt
        # Instance property access: ``obj.PascalProperty`` -- guess that the
        # property's type matches its name (.NET convention; very common
        # for typed wrapper/sub-object properties like
        # ``request.RequestMetrics``, ``ctx.AuthContext``). False positives
        # for primitive-named properties (``Count``, ``Length``, ``Name``)
        # are tolerated -- the qualified form they produce doesn't match
        # any real call line at the AST stage.
        if name.type == "identifier":
            ptxt = _text(name, src).strip()
            if ptxt and ptxt[:1].isupper():
                return ptxt
        return ""

    if t == "conditional_expression":
        # ``cond ? A : B`` -- try each branch in turn; the first that yields
        # a type wins. Captures patterns like
        # ``var x = useNear ? group.Near : group.Far`` where both branches
        # are property accesses (instance heuristic above resolves them
        # individually).
        for field in ("consequence", "alternative"):
            branch = expr.child_by_field_name(field)
            if branch is not None:
                r = _infer_var_type(branch, src, scope_map)
                if r:
                    return r
        return ""

    return ""


def _get_init_expr(declarator):
    children = declarator.children
    if len(children) >= 3 and children[1].type == "=":
        return children[2]
    return None


def _add_variable_decl(node, src, scope_map: dict,
                       explicit: list, var_inferred: list) -> None:
    """Split a ``variable_declaration`` into explicit / var-inferred buckets.

    ``explicit`` and ``var_inferred`` are appended to so the caller can apply
    explicit declarations first (so element-access var-inference can resolve
    against known array types).
    """
    tn = node.child_by_field_name("type")
    if tn is None:
        return
    ttxt = _text(tn, src).strip()
    is_var = (ttxt == "var" or tn.type == "implicit_type")
    for decl in _find_all(node, lambda x: x.type == "variable_declarator"):
        vn = decl.child_by_field_name("name")
        if vn is None:
            continue
        if is_var:
            var_inferred.append((vn, _get_init_expr(decl)))
        else:
            explicit.append((vn, ttxt))


def _collect_scope_locals(scope_node, src, scope_map: dict,
                          scope_maps: dict, file_map: dict) -> None:
    """Populate scope_map with every variable declared **directly** in scope_node.

    Declarations inside nested scope nodes (other blocks, catch clauses,
    nested methods, lambdas, ...) belong to their own maps and are skipped.
    The resolver walks the parent chain at lookup time, so an inner scope
    transparently inherits names from outer scopes without needing the
    inner map to copy them.

    ``scope_maps`` and ``file_map`` are the partial-state used for
    cross-scope lookups at construction time -- chiefly to resolve
    ``foreach (var x in coll)`` where ``coll`` is a field or outer-method
    parameter and the iterator type can be derived from its element type.
    """
    nt = scope_node.type

    # -- Method-like nodes own their parameters; their body is a separate
    #    block scope that handles its own locals.
    if nt in _PARAMETERIZED_SCOPES:
        params = scope_node.child_by_field_name("parameters")
        if params is not None:
            for p in _find_all(params, lambda n: n.type == "parameter"):
                pt = p.child_by_field_name("type")
                pn = p.child_by_field_name("name")
                if pt and pn:
                    _scope_add(scope_map, _text(pn, src).strip(),
                               _text(pt, src).strip())
        # Lambdas may have an expression body (no block) -- pattern/decl
        # bindings inside the expression head still bind into this scope.
        body = scope_node.child_by_field_name("body")
        if body is not None and body.type != "block":
            _absorb_pattern_bindings(body, src, scope_map)
        return

    # -- catch_clause: (TypeName ident) declares ``ident: TypeName``.
    if nt == "catch_clause":
        decl = next((c for c in scope_node.children if c.type == "catch_declaration"), None)
        if decl is not None:
            idents = [c for c in decl.children if c.type == "identifier"]
            if len(idents) >= 2:
                _scope_add(scope_map, _text(idents[1], src).strip(),
                           _text(idents[0], src).strip())
        return

    # -- for_statement: initializer may carry a variable_declaration.
    if nt == "for_statement":
        explicit: list = []
        var_inferred: list = []
        for c in scope_node.children:
            if c.type == "variable_declaration":
                _add_variable_decl(c, src, scope_map, explicit, var_inferred)
        for vn, ttxt in explicit:
            _scope_add(scope_map, _text(vn, src).strip(), ttxt)
        for vn, expr in var_inferred:
            inferred = _infer_var_type(expr, src, scope_map)
            if inferred:
                _scope_add(scope_map, _text(vn, src).strip(), inferred)
        return

    # -- foreach_statement: declares ``left: type``.
    if nt == "foreach_statement":
        tn = scope_node.child_by_field_name("type")
        nm = scope_node.child_by_field_name("left")
        if tn is None or nm is None:
            return
        if tn.type != "implicit_type":
            _scope_add(scope_map, _text(nm, src).strip(), _text(tn, src).strip())
            return
        # ``foreach (var x in coll)`` -- derive x's type from coll's
        # collection element type. We need to look up ``coll`` across the
        # enclosing scopes (it's commonly a field or method param, not a
        # local in the foreach itself), which means consulting the partial
        # scope state and the file map.
        coll = scope_node.child_by_field_name("right")
        if coll is None or coll.type != "identifier":
            return
        coll_name = _text(coll, src).strip()
        coll_type = _walk_partial_scopes(coll_name, scope_node, scope_maps, file_map)
        if not isinstance(coll_type, str):
            return
        elem = _collection_element_type(coll_type)
        if elem:
            _scope_add(scope_map, _text(nm, src).strip(), elem)
        return

    # -- using_statement: ``using (var x = ...)`` or ``using (T x = ...)``.
    if nt == "using_statement":
        explicit = []
        var_inferred = []
        for c in scope_node.children:
            if c.type == "variable_declaration":
                _add_variable_decl(c, src, scope_map, explicit, var_inferred)
        for vn, ttxt in explicit:
            _scope_add(scope_map, _text(vn, src).strip(), ttxt)
        for vn, expr in var_inferred:
            inferred = _infer_var_type(expr, src, scope_map)
            if inferred:
                _scope_add(scope_map, _text(vn, src).strip(), inferred)
        return

    # -- block: walk direct children, stopping at nested scope nodes. Picks
    #    up local_declaration_statement (variable_declaration), declaration
    #    patterns inside expressions, and out-var declarations.
    explicit = []
    var_inferred = []
    pattern_nodes: list = []
    decl_expr_nodes: list = []

    stack = list(scope_node.children)
    while stack:
        n = stack.pop()
        sub_nt = n.type
        if sub_nt in _SCOPE_NODES:
            continue  # nested scope owns its declarations
        if sub_nt == "variable_declaration":
            _add_variable_decl(n, src, scope_map, explicit, var_inferred)
        elif sub_nt in ("declaration_pattern", "recursive_pattern"):
            pattern_nodes.append(n)
        elif sub_nt == "declaration_expression":
            decl_expr_nodes.append(n)
        stack.extend(n.children)

    for vn, ttxt in explicit:
        _scope_add(scope_map, _text(vn, src).strip(), ttxt)

    for n in pattern_nodes:
        tn = n.child_by_field_name("type")
        nm = n.child_by_field_name("name")
        if tn is not None and nm is not None:
            _scope_add(scope_map, _text(nm, src).strip(), _text(tn, src).strip())

    for n in decl_expr_nodes:
        tn = n.child_by_field_name("type")
        nm = n.child_by_field_name("name")
        if tn is not None and nm is not None and tn.type != "implicit_type":
            _scope_add(scope_map, _text(nm, src).strip(), _text(tn, src).strip())

    for vn, expr in var_inferred:
        inferred = _infer_var_type(expr, src, scope_map)
        if inferred:
            _scope_add(scope_map, _text(vn, src).strip(), inferred)


def _absorb_pattern_bindings(node, src, scope_map: dict) -> None:
    """Collect declaration-pattern and out-var bindings inside ``node``.

    Used for lambdas with an expression body (`x => x is T t ? t.M() : null`).
    Walks past nested scope nodes so nested lambdas don't leak.
    """
    stack = [node]
    while stack:
        n = stack.pop()
        nt = n.type
        if n is not node and nt in _SCOPE_NODES:
            continue
        if nt in ("declaration_pattern", "recursive_pattern", "declaration_expression"):
            tn = n.child_by_field_name("type")
            nm = n.child_by_field_name("name")
            if tn is not None and nm is not None and tn.type != "implicit_type":
                _scope_add(scope_map, _text(nm, src).strip(),
                           _text(tn, src).strip())
        stack.extend(n.children)


def _walk_partial_scopes(name: str, start_node, scope_maps: dict,
                         file_map: dict):
    """Walk up the parent chain looking for ``name`` in the partial state.

    Called from inside scope construction -- ``scope_maps`` only contains
    scopes built so far. The DFS-preorder iteration in
    ``_build_var_type_map`` guarantees that every *enclosing* scope is
    already built by the time we look at an inner one, so this works for
    cross-scope name resolution at construction time (e.g. resolving a
    field/property/outer-method-param from inside a foreach).
    """
    if not name:
        return None
    p = start_node
    while p is not None:
        sm = scope_maps.get(p.id)
        if sm is not None and name in sm:
            return sm[name]
        p = p.parent
    return file_map.get(name)


def _collection_element_type(type_txt: str) -> str:
    """Best-guess element type for a collection type string.

    Handles ``T[]`` (most precise), and PascalCase-generic forms with one
    type arg (``List<T>``, ``IEnumerable<T>``, ``HashSet<T>``, ...). For
    multi-arg generics (``Dictionary<K, V>``, ``KeyValuePair<K, V>``) the
    element type isn't a single name, so we return "" -- callers fall back
    to leaving the iterator unresolved rather than guessing wrong.
    """
    t = type_txt.strip()
    if not t:
        return ""
    if t.endswith("[]"):
        return t[:-2].strip()
    lt = t.find("<")
    if lt > 0 and t.endswith(">"):
        inner = t[lt + 1:-1].strip()
        if inner and "," not in inner:
            return inner
    return ""


def _build_var_type_map(tree, src) -> _VarTypeMap:
    """Build a block-scoped variable-name -> resolved-type map for the file.

    File scope holds fields/properties/events declared at the type level;
    every block/catch/loop/method gets its own per-scope map layered on
    top. Conflicting types within one scope are sentinelled to ``None``:
    the resolver still reports the name as "known but ambiguous" so the
    caller can choose not to emit a qualified form.
    """
    file_map: dict[str, str | None] = {}

    # Fields and properties at the type level -- visible inside every method
    # of the enclosing type, so they belong to file scope for resolution.
    for node in _find_all(tree.root_node, lambda n: n.type in (
            "field_declaration", "event_field_declaration")):
        var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
        if not var_decl:
            continue
        tn = var_decl.child_by_field_name("type")
        if not tn:
            continue
        ttxt = _text(tn, src).strip()
        for decl in _find_all(var_decl, lambda x: x.type == "variable_declarator"):
            vn = decl.child_by_field_name("name")
            if vn:
                _scope_add(file_map, _text(vn, src).strip(), ttxt)

    for node in _find_all(tree.root_node, lambda n: n.type == "property_declaration"):
        tn = node.child_by_field_name("type")
        nm = node.child_by_field_name("name")
        if tn and nm:
            _scope_add(file_map, _text(nm, src).strip(), _text(tn, src).strip())

    # Per-scope maps for every block/method-like/lambda/loop/catch. We rely
    # on ``_find_all``'s DFS-preorder order: an outer scope is processed
    # before any inner scope it contains, so when a foreach (or other
    # cross-scope inference) walks up to resolve a name, the parent
    # scope's map is already populated.
    scope_maps: dict[int, dict[str, str | None]] = {}
    for node in _find_all(tree.root_node, lambda n: n.type in _SCOPE_NODES):
        m: dict[str, str | None] = {}
        _collect_scope_locals(node, src, m, scope_maps, file_map)
        scope_maps[node.id] = m

    return _VarTypeMap(scope_maps, file_map)


# -- Shared traversal helpers ---------------------------------------------------

def _iter_single_field_locals(tree, src, type_name, node_types, name_field, *,
                               skip_implicit=False):
    """
    Yield (node, type_txt, var_txt) for each node whose AST type is in
    `node_types`, whose 'type' field contains `type_name`, and whose variable
    name is in `name_field`.  When `skip_implicit` is True, nodes with an
    implicit_type (var) are skipped.
    """
    for node in _find_all(tree.root_node, lambda n: n.type in set(node_types)):
        type_node = node.child_by_field_name("type")
        if not type_node:
            continue
        if skip_implicit and type_node.type == "implicit_type":
            continue
        type_txt = _text(type_node, src).strip()
        if type_name not in _type_names(type_txt):
            continue
        var_node = node.child_by_field_name(name_field)
        if var_node:
            yield node, type_txt, _text(var_node, src).strip()


def _iter_all_locals(tree, src, type_name):
    """
    Yield (anchor_node, type_txt, var_txt) for every typed local binding of
    `type_name`, covering:
      - local_declaration_statement / using_statement / for_statement
      - foreach iteration variables
      - out variables and tuple deconstruction (declaration_expression)
      - is-pattern and switch-case pattern bindings (declaration_pattern)
      - catch-clause variable bindings
    """
    # Declarations: local vars, using-statement vars, for-loop init vars
    for node in _find_all(tree.root_node, lambda n: n.type in (
            "local_declaration_statement", "using_statement", "for_statement")):
        var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
        if not var_decl:
            continue
        type_node = var_decl.child_by_field_name("type")
        if not type_node:
            continue
        type_txt = _text(type_node, src).strip()
        if type_name not in _type_names(type_txt):
            continue
        for var in _find_all(var_decl, lambda n: n.type == "variable_declarator"):
            vn = var.child_by_field_name("name")
            if vn:
                yield node, type_txt, _text(vn, src).strip()
    # foreach (Connection item in arr)
    yield from _iter_single_field_locals(
        tree, src, type_name, ("foreach_statement",), "left", skip_implicit=True)
    # out variables: TryOpen(out Connection opened)
    # tuple deconstruction: (Connection first, Connection second) = expr
    yield from _iter_single_field_locals(
        tree, src, type_name, ("declaration_expression",), "name", skip_implicit=True)
    # is-pattern: if (s is Circle c), switch case Circle ci:
    # recursive pattern: if (s is Circle { Prop: v } c) -- same type/name fields
    yield from _iter_single_field_locals(
        tree, src, type_name, ("declaration_pattern", "recursive_pattern"), "name")
    # catch clause: catch (Connection ex)
    for node in _find_all(tree.root_node, lambda n: n.type == "catch_clause"):
        decl = next((c for c in node.children if c.type == "catch_declaration"), None)
        if not decl:
            continue
        idents = [c for c in decl.children if c.type == "identifier"]
        if len(idents) < 2:
            continue  # no variable name, e.g. catch (IOException) with no binding
        type_txt = _text(idents[0], src).strip()
        if type_name not in _type_names(type_txt):
            continue
        yield node, type_txt, _text(idents[1], src).strip()


def _iter_cast_nodes(tree, src, type_name):
    """
    Yield each cast_expression and as_expression node (not inside a literal)
    whose target type matches `type_name`, deduplicated by source row.
    cast_expression uses the 'type' field; as_expression uses 'right'.
    """
    seen_rows = set()
    _CAST_SPECS = (
        ("cast_expression", "type"),
        ("as_expression",   "right"),
    )
    for node_type, type_field in _CAST_SPECS:
        for node in _find_all(tree.root_node, lambda n, nt=node_type: n.type == nt):
            if _in_literal(node):
                continue
            type_node = node.child_by_field_name(type_field)
            if not type_node:
                continue
            if _strip_generic(_unqualify(_text(type_node, src).strip())) != type_name:
                continue
            row = node.start_point[0]
            if row in seen_rows:
                continue
            seen_rows.add(row)
            yield node


def _iter_initializer_members(tree, src):
    """
    Yield (assign_node, type_node, lhs_ident) for each assignment_expression
    inside an object_creation_expression initializer block:
        new Widget { Value = 5, Name = "test" }
    """
    for node in _find_all(tree.root_node, lambda n: n.type == "object_creation_expression"):
        init = next((c for c in node.children if c.type == "initializer_expression"), None)
        if not init:
            continue
        type_node = node.child_by_field_name("type")
        for assign in _find_all(init, lambda n: n.type == "assignment_expression"):
            lhs = assign.children[0] if assign.children else None
            if lhs and lhs.type == "identifier":
                yield assign, type_node, lhs


def _iter_with_members(tree, src):
    """
    Yield (with_initializer_node, src_ident, prop_ident) for each member in a
    with-expression:
        obj with { Prop = val }
    src_ident is the source object identifier; prop_ident is the property name.
    """
    for node in _find_all(tree.root_node, lambda n: n.type == "with_expression"):
        src_ident = node.children[0] if node.children else None
        if not src_ident or src_ident.type != "identifier":
            continue
        for wi in node.children:
            if wi.type != "with_initializer":
                continue
            prop = wi.children[0] if wi.children else None
            if prop and prop.type == "identifier":
                yield wi, src_ident, prop


# -- Data extraction functions -------------------------------------------------

def _q_namespace(src, idx: TreeIndex) -> str:
    """Extract the primary namespace name."""
    ns_nodes = idx.of("namespace_declaration", "file_scoped_namespace_declaration")
    if ns_nodes:
        name_node = ns_nodes[0].child_by_field_name("name")
        if name_node:
            return _text(name_node, src)
    return ""


def _q_classes_data(src, idx: TreeIndex) -> list:
    """Return list[ClassInfo] for all type declarations."""
    results = []
    for node in idx.of(*_TYPE_DECL_NODES):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind  = _node_kind(node)
        name  = _text(name_node, src).strip()
        bases = _base_type_names(node, src)
        results.append(ClassInfo(line=_line(node), end_line=_end_line(node),
                                 name=name, kind=kind, bases=bases,
                                 visibility=_cs_type_visibility(node)))
    return results


def _q_methods_data(src, idx: TreeIndex) -> list:
    """Return list[MethodInfo] for all member declarations."""
    results = []
    for node in idx.of(*_MEMBER_DECL_NODES):
        ln       = _line(node)
        end      = _end_line(node)
        toks     = _sig_tokens(node, src)
        vis      = _cs_member_visibility(node)
        if node.type == "field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    name = _text(vn, src).strip()
                    results.append(MethodInfo(line=ln, end_line=end, name=name, kind="field",
                                              sig=f"{type_txt} {name}".strip(),
                                              sig_tokens=toks, visibility=vis))
        elif node.type == "property_declaration":
            type_node = node.child_by_field_name("type")
            name_node = node.child_by_field_name("name")
            if name_node:
                type_txt = _text(type_node, src).strip() if type_node else ""
                name = _text(name_node, src).strip()
                results.append(MethodInfo(line=ln, end_line=end, name=name, kind="prop",
                                          sig=f"{type_txt} {name}".strip(),
                                          sig_tokens=toks, visibility=vis))
        elif node.type == "event_declaration":
            type_node = node.child_by_field_name("type")
            name_node = node.child_by_field_name("name")
            if name_node:
                type_txt = _text(type_node, src).strip() if type_node else ""
                name = _text(name_node, src).strip()
                results.append(MethodInfo(line=ln, end_line=end, name=name, kind="event",
                                          sig=f"{type_txt} {name}".strip(),
                                          sig_tokens=toks, visibility=vis))
        elif node.type == "event_field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    name = _text(vn, src).strip()
                    results.append(MethodInfo(line=ln, end_line=end, name=name, kind="event",
                                              sig=f"{type_txt} {name}".strip(),
                                              sig_tokens=toks, visibility=vis))
        elif node.type in ("method_declaration", "local_function_statement"):
            sig = _build_sig(node, src)
            if sig:
                ret_node = node.child_by_field_name("returns") or node.child_by_field_name("type")
                ret_txt = _text(ret_node, src).strip() if ret_node else ""
                name_node = node.child_by_field_name("name")
                name = _text(name_node, src).strip() if name_node else ""
                params_node = node.child_by_field_name("parameters")
                param_types = []
                if params_node:
                    for p in _find_all(params_node, lambda n: n.type == "parameter"):
                        pt = p.child_by_field_name("type")
                        if pt:
                            param_types.append(_text(pt, src).strip())
                results.append(MethodInfo(line=ln, end_line=end, name=name, kind="method",
                                          sig=sig, return_type=ret_txt,
                                          param_types=param_types,
                                          sig_tokens=toks, visibility=vis))
        elif node.type == "constructor_declaration":
            sig = _build_sig(node, src)
            if sig:
                name_node = node.child_by_field_name("name")
                name = _text(name_node, src).strip() if name_node else ""
                params_node = node.child_by_field_name("parameters")
                param_types = []
                if params_node:
                    for p in _find_all(params_node, lambda n: n.type == "parameter"):
                        pt = p.child_by_field_name("type")
                        if pt:
                            param_types.append(_text(pt, src).strip())
                results.append(MethodInfo(line=ln, end_line=end, name=name, kind="ctor",
                                          sig=sig, param_types=param_types,
                                          sig_tokens=toks, visibility=vis))
    return results


def _q_fields_data(src, idx: TreeIndex) -> list:
    """Return list[FieldInfo] for all field and property declarations."""
    results = []
    for node in idx.of("field_declaration", "property_declaration"):
        ln   = _line(node)
        end  = _end_line(node)
        toks = _sig_tokens(node, src)
        vis  = _cs_member_visibility(node)
        if node.type == "field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    name = _text(vn, src).strip()
                    results.append(FieldInfo(line=ln, end_line=end, name=name, kind="field",
                                             field_type=type_txt,
                                             sig_tokens=toks, visibility=vis))
        else:
            type_node = node.child_by_field_name("type")
            type_txt  = _text(type_node, src).strip() if type_node else ""
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(name_node, src).strip()
                results.append(FieldInfo(line=ln, end_line=end, name=name, kind="prop",
                                         field_type=type_txt,
                                         sig_tokens=toks, visibility=vis))
    return results


def _q_usings_data(src, idx: TreeIndex) -> list:
    """Return list[ImportInfo] for all using directives."""
    results = []
    for node in idx.of("using_directive"):
        full = _text(node, src).strip().rstrip(";")
        namespace = ""
        for child in node.named_children:
            if child.type in ("identifier", "qualified_name"):
                namespace = _text(child, src).split(".")[0]
                break
        results.append(ImportInfo(line=_line(node), text=full, module=namespace))
    return results


def _q_attrs_data(src, idx: TreeIndex, attr_name=None) -> list:
    """Return list[AttrInfo] for all attribute decorators."""
    results = []
    for node in idx.of("attribute"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        aname        = _text(name_node, src).strip()
        aname_short  = aname[:-len("Attribute")] if aname.endswith("Attribute") else aname
        aname_unqual = _unqualify(aname_short)
        if attr_name:
            if aname_unqual != attr_name and aname_short != attr_name and aname != attr_name:
                continue
        args_node = next((c for c in node.named_children
                          if c.type == "attribute_argument_list"), None)
        args_txt = _text(args_node, src).strip() if args_node else ""
        results.append(AttrInfo(line=_line(node), text=f"[{aname}]{args_txt}",
                                attr_name=aname_unqual))
    return results


def _q_all_call_site_infos(src, idx: TreeIndex, var_map: _VarTypeMap | None = None) -> list:
    """Extract call sites as ``CallSiteInfo`` objects.

    Captures the literal identifier receiver when one is present (``Foo`` in
    ``Foo.Bar()`` or ``repo`` in ``repo.Save()``) and, when ``var_map`` is
    provided, resolves that receiver to its declared/inferred type so the
    indexer can emit a stable ``Type.Method`` token. Receivers whose name
    isn't in scope or maps to conflicting types in the same scope leave
    ``resolved_type`` empty -- the bare-name + literal-receiver fallback in
    the indexer keeps the call discoverable either way.
    """
    result = []
    for node in idx.of("invocation_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node:
            if fn_node.type == "member_access_expression":
                nn   = fn_node.child_by_field_name("name")
                expr = fn_node.child_by_field_name("expression")
                if nn:
                    receiver = _text(expr, src).strip() if (expr and expr.type == "identifier") else ""
                    resolved = ""
                    if receiver and var_map is not None:
                        rt = var_map.resolve_at(receiver, node)
                        if rt:
                            resolved = _strip_generic(rt.rsplit(".", 1)[-1])
                    result.append(CallSiteInfo(
                        name=_text(nn, src).strip(),
                        receiver=receiver,
                        resolved_type=resolved,
                    ))
            elif fn_node.type == "identifier":
                result.append(CallSiteInfo(name=_text(fn_node, src).strip()))
    for name in _collect_ctor_names(idx, src):
        result.append(CallSiteInfo(name=name))
    return result


def _q_all_cast_types_data(src, idx: TreeIndex) -> list:
    """Extract all cast target type strings for indexing."""
    types = []
    for node in idx.of("cast_expression"):
        type_node = node.child_by_field_name("type")
        if type_node:
            types.append(_text(type_node, src).strip())
    return types


def _q_all_member_accesses_data(src, idx: TreeIndex) -> list:
    """Extract non-invocation member access names for indexing."""
    _invocation_fn_ids = {
        id(node.child_by_field_name("function"))
        for node in idx.of("invocation_expression")
        if node.child_by_field_name("function") is not None
        and node.child_by_field_name("function").type == "member_access_expression"
    }
    names = []
    for node in idx.of("member_access_expression"):
        if id(node) not in _invocation_fn_ids:
            nn = node.child_by_field_name("name")
            if nn:
                names.append(_text(nn, src).strip())
    return names


def _q_all_local_types_data(src, idx: TreeIndex) -> list:
    """Extract all local variable type strings for indexing."""
    types = []
    for node in idx.of("local_declaration_statement"):
        var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
        if var_decl:
            type_node = var_decl.child_by_field_name("type")
            if type_node:
                types.append(_text(type_node, src).strip())
    return types


# -- Query functions ------------------------------------------------------------

def _parse_visibility_filter(visibility):
    """Normalise the optional visibility filter into a set of canonical
    tokens (or None when no filter is requested). Accepts a single value
    (``"public"``), a comma-separated string (``"public,internal"``), or
    any iterable. Unknown tokens are dropped silently -- callers shouldn't
    crash on a typo, just get back nothing matching their typo."""
    if not visibility:
        return None
    if isinstance(visibility, str):
        toks = [t.strip().lower() for t in visibility.split(",")]
    else:
        toks = [str(t).strip().lower() for t in visibility]
    keep = {t for t in toks if t}
    return keep or None


def _visibility_keep(info_visibility: str, allowed) -> bool:
    """True when ``info_visibility`` passes the optional filter.

    Empty string on the info means "language didn't capture a visibility"
    and is treated as a hard miss -- searching ``visibility="public"`` over
    e.g. SQL files never matches, which is the right answer.
    """
    if allowed is None:
        return True
    return bool(info_visibility) and info_visibility in allowed


def q_classes(src, tree, lines, visibility=None):
    allowed = _parse_visibility_filter(visibility)
    return [(_r.line, _r.end_line, _r.text)
            for _r in _q_classes_data(src, _CsIndex(src, tree, _TYPE_DECL_NODES))
            if _visibility_keep(getattr(_r, "visibility", ""), allowed)]


def q_methods(src, tree, lines, visibility=None):
    allowed = _parse_visibility_filter(visibility)
    return [(_r.line, _r.end_line, _r.text)
            for _r in _q_methods_data(src, _CsIndex(src, tree, _MEMBER_DECL_NODES))
            if _visibility_keep(getattr(_r, "visibility", ""), allowed)]


def q_fields(src, tree, lines, visibility=None):
    allowed = _parse_visibility_filter(visibility)
    return [(_r.line, _r.end_line, _r.text)
            for _r in _q_fields_data(src, _CsIndex(src, tree, {"field_declaration", "property_declaration"}))
            if _visibility_keep(getattr(_r, "visibility", ""), allowed)]


def q_calls(src, tree, lines, method_name,
            enclosing_method=None, enclosing_class=None):
    if "." in method_name:
        qualifier, bare_name = method_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, method_name

    # Build the var-type map lazily -- only when a qualified pattern is
    # supplied. Bare-method searches don't need receiver resolution.
    var_map = _build_var_type_map(tree, src) if qualifier else None

    _qualifier_str: str = qualifier or ""

    def _qualifier_matches(expr_node) -> bool:
        """True if ``expr_node`` (the receiver of a member access) matches
        ``qualifier`` either by literal text or by resolved type."""
        if expr_node is None or not _qualifier_str:
            return False
        expr_txt = _text(expr_node, src).strip()
        if expr_txt == _qualifier_str or expr_txt.endswith("." + _qualifier_str):
            return True
        # Resolved-type fallback: look up an identifier receiver in the
        # method-scoped var-type map and compare its unqualified type name.
        if expr_node.type == "identifier" and var_map is not None:
            rt = var_map.resolve_at(expr_txt, expr_node)
            if isinstance(rt, str) and rt:
                resolved = _strip_generic(rt.rsplit(".", 1)[-1])
                if resolved == _qualifier_str:
                    return True
        return False

    def _report(node, name_node):
        """Build a result tuple pinpointing the method-name token.

        Reports the line of ``name_node`` (where the matched identifier
        actually appears) rather than the start of the surrounding
        invocation -- for chained calls like ``a.B().Method(...)`` that
        means the result lands on the line of ``Method``, not on
        whichever line the chain begins. Source text is the single line
        containing the name, prefixed with the enclosing scope chain
        (``[in TypeName.MemberName] ``) so agents don't need a follow-up
        ``at LINE:COL`` query to find out which class/method the call
        lives in.
        """
        anchor = name_node if name_node is not None else node
        row = anchor.start_point[0]
        line_text = lines[row].strip() if 0 <= row < len(lines) else ""
        if not line_text:
            # Fall back to a truncated render of the node when the source
            # row is empty/missing (defensive -- shouldn't happen for real
            # call sites).
            line_text = _truncate_raw(node, src)
        prefix = _scope_prefix(node, src)
        return (_line(anchor), f"{prefix}{line_text}" if prefix else line_text)

    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "invocation_expression"):
        if _in_literal(node):
            continue
        fn = node.child_by_field_name("function")
        if not fn:
            continue
        matched = None
        match_name_node = None
        if fn.type == "member_access_expression":
            nn   = fn.child_by_field_name("name")
            expr = fn.child_by_field_name("expression")
            if nn:
                matched = _strip_generic(_text(nn, src))
                match_name_node = nn
                if qualifier and matched == bare_name:
                    if not _qualifier_matches(expr):
                        matched = None
        elif fn.type == "conditional_access_expression":
            # f?.Method(...) -- method name is in the trailing
            # member_binding_expression; receiver is the ``condition``
            # field on the conditional_access_expression.
            binding = next((c for c in fn.children
                            if c.type == "member_binding_expression"), None)
            if binding:
                nn = binding.child_by_field_name("name")
                if nn:
                    matched = _strip_generic(_text(nn, src))
                    match_name_node = nn
                    if qualifier and matched == bare_name:
                        cond = fn.child_by_field_name("condition")
                        if not _qualifier_matches(cond):
                            matched = None
        elif fn.type in ("identifier", "generic_name"):
            if qualifier is None:
                nn = fn.child_by_field_name("name") if fn.type == "generic_name" else fn
                if nn:
                    matched = _strip_generic(_text(nn, src))
                    match_name_node = nn
        if matched == bare_name:
            if _passes_enclosing_filter(node, src,
                                        enclosing_method, enclosing_class):
                results.append(_report(node, match_name_node))

    if qualifier is None:
        for node in _find_all(tree.root_node, lambda n: n.type == "object_creation_expression"):
            if _in_literal(node):
                continue
            type_node = node.child_by_field_name("type")
            if not type_node:
                continue
            idents = _find_all(type_node, lambda n: n.type == "identifier")
            if not idents:
                continue
            if _strip_generic(_text(idents[-1], src)) == bare_name:
                if not _passes_enclosing_filter(
                        node, src, enclosing_method, enclosing_class):
                    continue
                # ``new T(...)`` -- anchor at the type name token.
                results.append(_report(node, idents[-1]))
    return results


def q_caller_of(src, tree, lines, method_name):
    """Group ``calls`` hits by the enclosing method.

    Where ``calls METHOD`` returns one row per call site (potentially many
    inside the same caller), ``caller_of METHOD`` collapses those into one
    row per (TypeName.MemberName) caller, with a count of how many call
    sites that caller contains. Useful for impact analysis ("who depends
    on this") without the per-site noise.

    Result text per caller: ``[in TypeName.MemberName]  (N call sites)``.
    Rows are emitted at the line of the *first* call site so the caller
    line ordering matches source ordering.
    """
    hits = q_calls(src, tree, lines, method_name)
    if not hits:
        return []
    # Group by the leading "[in TypeName.MemberName] " prefix that q_calls
    # already attaches. Hits without a prefix (calls outside any member,
    # extremely rare) bucket under "<top-level>".
    by_caller: dict[str, list[tuple[int, str]]] = {}
    for ln, txt in hits:
        if txt.startswith("[in ") and "] " in txt:
            scope_end = txt.index("] ")
            caller = txt[4:scope_end]
            snippet = txt[scope_end + 2:]
        else:
            caller = "<top-level>"
            snippet = txt
        by_caller.setdefault(caller, []).append((ln, snippet))
    # Emit one row per caller, anchored at its first hit's line.
    results = []
    for caller, sites in by_caller.items():
        first_line = sites[0][0]
        plural = "site" if len(sites) == 1 else "sites"
        results.append((first_line,
                        f"[in {caller}]  ({len(sites)} call {plural})"))
    results.sort(key=lambda x: x[0])
    return results


def q_callee_of(src, tree, lines, method_name):
    """List every callee invoked inside the method named ``method_name``.

    The inverse of ``caller_of``: given a method, walk its body and return
    one row per distinct callee name, with a count of how many times that
    callee is invoked inside this method. Useful for "what does this
    method depend on" / "what could be slow here" analysis without having
    to read the full body.

    Result text per callee: ``Callee  (N invocations)``. Constructor calls
    (``new T()``) are reported as ``T  (N invocations, ctor)``.
    """
    # Find every method-shaped declaration with the given name.
    target_types = {"method_declaration", "constructor_declaration",
                    "local_function_statement"}
    matches: list = []
    for node in _find_all(tree.root_node, lambda n: n.type in target_types):
        nm = node.child_by_field_name("name")
        if nm and _text(nm, src).strip() == method_name:
            matches.append(node)
    if not matches:
        return []

    results = []
    for method_node in matches:
        body = method_node.child_by_field_name("body")
        if body is None:
            continue
        counts: dict[tuple[str, bool], int] = {}
        first_seen: dict[tuple[str, bool], int] = {}
        for inv in _find_all(body, lambda n: n.type == "invocation_expression"):
            if _in_literal(inv):
                continue
            fn = inv.child_by_field_name("function")
            if fn is None:
                continue
            callee_name = ""
            if fn.type == "member_access_expression":
                nm = fn.child_by_field_name("name")
                if nm:
                    callee_name = _strip_generic(_text(nm, src))
            elif fn.type in ("identifier", "generic_name"):
                callee_name = _strip_generic(_text(fn, src))
            elif fn.type == "conditional_access_expression":
                binding = next((c for c in fn.children
                                if c.type == "member_binding_expression"), None)
                if binding:
                    nm = binding.child_by_field_name("name")
                    if nm:
                        callee_name = _strip_generic(_text(nm, src))
            if not callee_name:
                continue
            key = (callee_name, False)
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, _line(inv))
        # Constructor calls -- ``new T(...)`` -- treated as callees of T.
        for ctor in _find_all(body, lambda n: n.type == "object_creation_expression"):
            if _in_literal(ctor):
                continue
            tn = ctor.child_by_field_name("type")
            if tn is None:
                continue
            idents = _find_all(tn, lambda n: n.type == "identifier")
            if not idents:
                continue
            callee_name = _strip_generic(_text(idents[-1], src))
            key = (callee_name, True)
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, _line(ctor))
        # Per-method anchor: emit each callee at its first-invocation line.
        for (callee, is_ctor), n in counts.items():
            anchor = first_seen[(callee, is_ctor)]
            plural = "invocation" if n == 1 else "invocations"
            suffix = ", ctor" if is_ctor else ""
            results.append(
                (anchor,
                 f"[in {method_name}]  {callee}  ({n} {plural}{suffix})"))
    results.sort(key=lambda x: x[0])
    return results


def q_accesses_of(src, tree, lines, member_name,
                  enclosing_method=None, enclosing_class=None):
    if "." in member_name:
        qualifier, bare_name = member_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, member_name

    results = []
    seen_rows = set()

    def _emit(containing_node, text):
        prefix = _scope_prefix(containing_node, src)
        results.append((_line(containing_node),
                        f"{prefix}{text}" if prefix else text))

    def _check_access(member_node, expr_node, containing_node):
        if not member_node:
            return
        if _strip_generic(_text(member_node, src)) != bare_name:
            return
        if qualifier:
            expr_txt = _text(expr_node, src).strip() if expr_node else ""
            if not (expr_txt == qualifier or expr_txt.endswith("." + qualifier)):
                return
        if not _passes_enclosing_filter(containing_node, src,
                                        enclosing_method, enclosing_class):
            return
        row = containing_node.start_point[0]
        if row in seen_rows:
            return
        seen_rows.add(row)
        _emit(containing_node, _truncate_raw(containing_node, src))

    for node in _find_all(tree.root_node, lambda n: n.type == "member_access_expression"):
        if _in_literal(node):
            continue
        _check_access(node.child_by_field_name("name"),
                      node.child_by_field_name("expression"),
                      node)

    for node in _find_all(tree.root_node, lambda n: n.type == "member_binding_expression"):
        if _in_literal(node):
            continue
        # member_binding_expression appears inside conditional_access_expression (?.member)
        _check_access(node.child_by_field_name("name"), None, node)

    # Object-initializer member assignments -- new Widget { Value = 5 }
    for assign, type_node, lhs in _iter_initializer_members(tree, src):
        if _in_literal(assign):
            continue
        if _strip_generic(_text(lhs, src)) != bare_name:
            continue
        obj_type = _unqualify(_text(type_node, src).strip()) if type_node else None
        if qualifier and obj_type != qualifier:
            continue
        if not _passes_enclosing_filter(assign, src,
                                        enclosing_method, enclosing_class):
            continue
        row = assign.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        _emit(assign, _truncate_raw(assign, src))

    # With-expression member mutations -- w with { Value = 10 }
    for wi, src_ident, prop in _iter_with_members(tree, src):
        if _strip_generic(_text(prop, src)) != bare_name:
            continue
        if qualifier and _text(src_ident, src).strip() != qualifier:
            continue
        if not _passes_enclosing_filter(wi, src,
                                        enclosing_method, enclosing_class):
            continue
        row = wi.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        _emit(wi, _truncate_raw(wi, src))

    results.sort(key=lambda x: x[0])
    return results


def q_implements(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        bases = _base_type_names(node, src)
        if not any(_strip_generic(_unqualify(b)) == type_name for b in bases):
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind      = _node_kind(node)
        name      = _text(name_node, src)
        base_list = next((c for c in node.children if c.type == "base_list"), None)
        base_str  = (_text(base_list, src).strip().lstrip(":").strip()
                     if base_list else ", ".join(bases))
        results.append((_line(node), f"[{kind}] {name} : {base_str}"))
    return results


def _q_uses_all(src, tree, lines, type_name):
    results  = []
    seen_rows = set()

    def _is_decl_name(node):
        p = node.parent
        if not p:
            return False
        nn = p.child_by_field_name("name")
        return nn is not None and nn.start_byte == node.start_byte

    def _is_invocation_target(node):
        p = node.parent
        if not p:
            return False
        if p.type == "invocation_expression":
            fn = p.child_by_field_name("function")
            if fn and fn.type == "identifier" and fn.start_byte == node.start_byte:
                return True
        if p.type == "member_access_expression":
            nn = p.child_by_field_name("name")
            if nn and nn.start_byte == node.start_byte:
                return True
        return False

    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != type_name:
            continue
        if _in_literal(node):
            continue
        if _is_decl_name(node):
            continue
        if _is_invocation_target(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def q_attrs(src, tree, lines, attr_name=None):
    return [(_r.line, _r.text) for _r in _q_attrs_data(src, _CsIndex(src, tree, {"attribute"}), attr_name)]


def q_usings(src, tree, lines):
    return [(_r.line, _r.text) for _r in _q_usings_data(src, _CsIndex(src, tree, {"using_directive"}))]


def q_declarations(src, tree, lines, name, include_body=False, symbol_kind=None,
                   visibility=None, head_lines=None):
    kind_nodes = SYMBOL_KIND_TO_NODES.get((symbol_kind or "").lower().strip())
    target_nodes = kind_nodes if kind_nodes is not None else (_TYPE_DECL_NODES | _MEMBER_DECL_NODES)
    allowed = _parse_visibility_filter(visibility)
    # head_lines normalisation: None / 0 / negative means "no truncation".
    try:
        head_n = int(head_lines) if head_lines is not None else None
    except (TypeError, ValueError):
        head_n = None
    if head_n is not None and head_n <= 0:
        head_n = None
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in target_nodes):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        if allowed is not None:
            # Use the same defaults as the indexer so the AST stage stays
            # consistent with the index pre-filter -- type kinds get the
            # type-level default, members get the member-level default.
            if node.type in _TYPE_DECL_NODES:
                vis = _cs_type_visibility(node)
            else:
                vis = _cs_member_visibility(node)
            if not _visibility_keep(vis, allowed):
                continue
        kind      = _node_kind(node)
        start_row = node.start_point[0]
        end_row   = node.end_point[0]
        body_node = node.child_by_field_name("body")
        if body_node and not include_body:
            sig_end_row = body_node.start_point[0]
            content_lines = lines[start_row:sig_end_row]
            content = "\n".join(content_lines).rstrip()
        else:
            content_lines = lines[start_row:end_row + 1]
            content = "\n".join(content_lines)
        # head_lines truncation: clip the content (signature + body together)
        # to the first N source lines, appending a "... +K more lines" tail
        # marker so the agent knows there's more to fetch if it wants.
        if head_n is not None and head_n < len(content_lines):
            kept = content_lines[:head_n]
            remaining = len(content_lines) - head_n
            content = "\n".join(kept).rstrip()
            content = f"{content}\n... +{remaining} more lines"
        header = f"[{kind}] {name} {start_row + 1}-{end_row + 1}:"
        results.append((_line(node), f"{header}\n{content}"))
    return results


_SCOPE_NODE_NAMES = (
    _TYPE_DECL_NODES
    | _MEMBER_DECL_NODES
    | {"namespace_declaration", "file_scoped_namespace_declaration"}
)


def _field_declarator_name(field_node, target_row: int, target_col: int):
    """Find the ``variable_declarator``'s name inside a field_declaration or
    event_field_declaration. When the field declares multiple variables on
    one line (``int a, b, c``), pick the declarator whose range contains
    the target; fall back to the first when none does (e.g. cursor sits on
    a modifier keyword like ``readonly``)."""
    var_decl = next((c for c in field_node.children
                     if c.type == "variable_declaration"), None)
    if var_decl is None:
        return None
    declarators = [c for c in var_decl.children
                   if c.type == "variable_declarator"]
    if not declarators:
        return None
    for d in declarators:
        sr, sc = d.start_point
        er, ec = d.end_point
        if (sr, sc) <= (target_row, target_col) < (er, ec):
            return d.child_by_field_name("name")
    return declarators[0].child_by_field_name("name")


def q_at(src, tree, lines, position: str):
    """Identify the symbol and enclosing scope chain at ``line:col``.

    ``position`` is parsed as ``"LINE:COL"`` (1-indexed, matching editor URLs).
    Returns a single match whose text contains:

      * the token/node at the position (the deepest AST node covering it),
      * the chain of enclosing named declarations (innermost-first), each
        with its line range.

    Useful for resolving stack traces, test failures, and review comments
    that mention a file:line[:col] location.
    """
    try:
        line_str, _, col_str = position.partition(":")
        target_row = int(line_str) - 1
        target_col = int(col_str) - 1 if col_str else 0
    except (TypeError, ValueError):
        return []
    if target_row < 0:
        return []

    # Walk the tree finding the deepest node whose range covers the target
    # (row, col). Tree-sitter rows/cols are 0-indexed; ``end_point`` is the
    # position AFTER the last char (exclusive) -- half-open range [start, end).
    root = tree.root_node
    er, ec = root.end_point
    if (target_row, target_col) >= (er, ec):
        return []

    def _contains(n) -> bool:
        sr, sc = n.start_point
        er2, ec2 = n.end_point
        if (target_row, target_col) < (sr, sc):
            return False
        if (target_row, target_col) >= (er2, ec2):
            return False
        return True

    cursor = root
    deepest = None
    while True:
        next_child = None
        for child in cursor.children:
            if _contains(child):
                next_child = child
                break
        if next_child is None:
            break
        cursor = next_child
        deepest = next_child

    if deepest is None:
        # Position is inside the file but no concrete node covers it
        # (e.g. trailing whitespace). Not useful to report the root.
        return []

    # Collect named scope ancestors, innermost-first.
    scopes = []
    walker = deepest
    while walker is not None:
        if walker.type in _SCOPE_NODE_NAMES:
            name_node = walker.child_by_field_name("name")
            # ``field_declaration`` / ``event_field_declaration`` carry their
            # name inside a nested ``variable_declaration`` -> ``variable_declarator``
            # rather than on a direct ``name`` field. Pick the declarator
            # whose range contains the target, else the first declarator.
            if name_node is None and walker.type in (
                    "field_declaration", "event_field_declaration"):
                name_node = _field_declarator_name(walker, target_row, target_col)
            if name_node:
                scopes.append({
                    "kind":  _node_kind(walker),
                    "name":  _text(name_node, src).strip(),
                    "start": walker.start_point[0] + 1,
                    "end":   walker.end_point[0] + 1,
                })
        walker = walker.parent

    # Render -- one entry, with the chain in the text.
    token = _text(deepest, src)
    if len(token) > 60:
        token = token[:57] + "..."
    token = token.replace("\n", " ").strip() or "(no text)"
    parts = [f"{deepest.type}: {token!r}"]
    for sc in scopes:
        parts.append(f"  in [{sc['kind']}] {sc['name']}  (lines {sc['start']}-{sc['end']})")
    text = "\n".join(parts)
    out_line = deepest.start_point[0] + 1
    return [(out_line, text)]


def q_body(src, tree, lines, name, symbol_kind=None, head_lines=None):
    """Return the full source of every member declaration named ``name``.

    Sugar for ``q_declarations(..., include_body=True)`` with a single-name
    intent: the agent says "give me the source of SaveChanges" and gets the
    whole method block (or every overload, if more than one matches).

    ``head_lines`` truncates each body to the first N source lines (header
    line excluded; truncation indicator appended). Useful when scanning
    many bodies at once and the signature plus the first few lines is
    enough.
    """
    return q_declarations(src, tree, lines, name,
                          include_body=True, symbol_kind=symbol_kind,
                          head_lines=head_lines)


def q_params(src, tree, lines, method_name):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("method_declaration",
                                               "constructor_declaration",
                                               "local_function_statement")):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != method_name:
            continue
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in _find_all(params_node, lambda n: n.type == "parameter"):
            pt   = p.child_by_field_name("type")
            pn   = p.child_by_field_name("name")
            pt_t = _text(pt, src).strip() if pt else ""
            pn_t = _text(pn, src).strip() if pn else ""
            df_t = ""
            children = p.children
            for idx, ch in enumerate(children):
                if not ch.is_named and _text(ch, src).strip() == "=" and idx + 1 < len(children):
                    df_t = f" = {_text(children[idx + 1], src).strip()}"
                    break
            mods = [_text(c, src) for c in children
                    if c.is_named and c.type in ("modifier", "parameter_modifier")]
            mod_t = " ".join(mods) + " " if mods else ""
            param_lines.append(f"  {mod_t}{pt_t} {pn_t}{df_t}".rstrip())
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


def _q_field_type(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("field_declaration",
                                               "event_field_declaration",
                                               "property_declaration")):
        if node.type in ("field_declaration", "event_field_declaration"):
            type_txt = _field_type(node, src)
            if type_name not in _type_names(type_txt):
                continue
            label = "[field]" if node.type == "field_declaration" else "[event]"
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    cls = _enclosing_type_name(node, src)
                    cls_prefix = f"[in {cls}] " if cls else ""
                    results.append((_line(node), f"{label} {type_txt} {_text(vn, src)}  {cls_prefix}".rstrip()))
        else:
            type_node = node.child_by_field_name("type")
            if not type_node:
                continue
            type_txt = _text(type_node, src).strip()
            if type_name not in _type_names(type_txt):
                continue
            name_node = node.child_by_field_name("name")
            if name_node:
                cls_prefix = _cls_prefix(node, src)
                results.append((_line(node), f"[prop]  {type_txt} {_text(name_node, src)}  {cls_prefix}".rstrip()))
    return results


def _q_param_type(src, tree, lines, type_name):
    results = []
    for mnode in _find_all(tree.root_node,
                           lambda n: n.type in ("method_declaration", "constructor_declaration",
                                                "local_function_statement", "delegate_declaration",
                                                "lambda_expression")):
        params_node = mnode.child_by_field_name("parameters")
        if not params_node:
            continue
        name_node = mnode.child_by_field_name("name")
        mname = _text(name_node, src).strip() if name_node else "<lambda>"
        kind  = mnode.type.replace("_declaration", "").replace("statement", "").replace("_", " ").strip()
        for p in _find_all(params_node, lambda n: n.type == "parameter"):
            pt = p.child_by_field_name("type")
            if not pt:
                continue
            pt_txt = _text(pt, src).strip()
            if type_name not in _type_names(pt_txt):
                continue
            pn = p.child_by_field_name("name")
            pn_txt = _text(pn, src).strip() if pn else ""
            mods = [_text(c, src) for c in p.children
                    if c.is_named and c.type in ("modifier", "parameter_modifier")]
            mod_t = " ".join(mods) + " " if mods else ""
            results.append((_line(p), f"[{kind}] {mname}({mod_t}{pt_txt} {pn_txt})"))
    return results


def _q_return_type(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("method_declaration",
                                               "constructor_declaration",
                                               "local_function_statement",
                                               "delegate_declaration")):
        # method_declaration exposes its return type via "returns"; all other
        # supported node types use "type".
        if node.type == "method_declaration":
            type_node = node.child_by_field_name("returns")
        else:
            type_node = node.child_by_field_name("type")
        ret_type_txt = _text(type_node, src).strip() if type_node else ""
        if type_name not in _type_names(ret_type_txt):
            continue
        name_node = node.child_by_field_name("name")
        mname = _text(name_node, src).strip() if name_node else "<anonymous>"
        params_node = node.child_by_field_name("parameters")
        param_parts = []
        if params_node:
            for p in _find_all(params_node, lambda n: n.type == "parameter"):
                pt = p.child_by_field_name("type")
                pn = p.child_by_field_name("name")
                pt_t = _text(pt, src).strip() if pt else ""
                pn_t = _text(pn, src).strip() if pn else ""
                param_parts.append(f"{pt_t} {pn_t}".strip())
        sig_text = f"{ret_type_txt} {mname}({', '.join(param_parts)})".strip()
        results.append((_line(node), sig_text))
    return results


def _q_local_type(src, tree, lines, type_name):
    results = [
        (_line(node), f"{_scope_prefix(node, src)}[local] {type_txt} {var_txt}")
        for node, type_txt, var_txt in _iter_all_locals(tree, src, type_name)
    ]
    results.sort(key=lambda x: x[0])
    return results


def _q_base_uses(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        bases = _base_type_names(node, src)
        if not any(type_name in _type_names(b) for b in bases):
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind      = _node_kind(node)
        name      = _text(name_node, src)
        base_list = next((c for c in node.children if c.type == "base_list"), None)
        base_str  = (_text(base_list, src).strip().lstrip(":").strip()
                     if base_list else ", ".join(bases))
        results.append((_line(node), f"[{kind}] {name} : {base_str}"))
    return results


def q_uses(src, tree, lines, type_name, uses_kind=None,
           enclosing_method=None, enclosing_class=None):
    k = (uses_kind or "all").lower().strip()
    # Apply the enclosing-scope filter as a post-pass for sub-modes whose
    # underlying helper doesn't accept the kwargs natively. The body-level
    # sub-modes (cast, locals) and the cross-cutting "all" mode already
    # benefit. Declaration-level sub-modes (field, param, return, base)
    # emit at the declaration line itself, which has no enclosing method
    # by definition -- filtering by ``enclosing_method`` would drop every
    # row, which matches the user's intent ("only inside method X").
    if k == "field":
        results = _q_field_type(src, tree, lines, type_name)
    elif k == "param":
        results = _q_param_type(src, tree, lines, type_name)
    elif k == "return":
        results = _q_return_type(src, tree, lines, type_name)
    elif k == "cast":
        return q_casts(src, tree, lines, type_name,
                       enclosing_method=enclosing_method,
                       enclosing_class=enclosing_class)
    elif k == "base":
        results = _q_base_uses(src, tree, lines, type_name)
    elif k == "locals":
        return _filter_by_enclosing(
            _q_local_type(src, tree, lines, type_name),
            tree, src, enclosing_method, enclosing_class)
    else:
        return _filter_by_enclosing(
            _q_uses_all(src, tree, lines, type_name),
            tree, src, enclosing_method, enclosing_class)
    return _filter_by_enclosing(
        results, tree, src, enclosing_method, enclosing_class)


def _filter_by_enclosing(rows, tree, src,
                         enclosing_method, enclosing_class):
    """Drop rows whose line falls outside the optional enclosing-method /
    enclosing-class scope. Returns the input unchanged when no filter is
    requested. Walks the AST once to map row line numbers to enclosing
    member/type names so the per-row check is O(1) lookups.
    """
    if not enclosing_method and not enclosing_class:
        return rows
    # Build a line -> (type_name, member_name) map by scanning every
    # type and member declaration once. Inner declarations shadow outer
    # ones for overlapping line ranges, so we walk types first (outer),
    # then members (inner).
    line_to_type: dict[int, str] = {}
    line_to_member: dict[int, str] = {}
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        nm = node.child_by_field_name("name")
        if nm is None:
            continue
        name = _text(nm, src).strip()
        for r in range(node.start_point[0] + 1, node.end_point[0] + 2):
            line_to_type[r] = name
    for node in _find_all(tree.root_node, lambda n: n.type in _MEMBER_DECL_NODES):
        nm = node.child_by_field_name("name")
        if nm is None and node.type in ("field_declaration", "event_field_declaration"):
            vd = next((c for c in node.children
                       if c.type == "variable_declaration"), None)
            if vd is not None:
                decl = next((c for c in vd.children
                             if c.type == "variable_declarator"), None)
                if decl is not None:
                    nm = decl.child_by_field_name("name")
        if nm is None:
            continue
        name = _text(nm, src).strip()
        for r in range(node.start_point[0] + 1, node.end_point[0] + 2):
            line_to_member[r] = name
    keep = []
    for row in rows:
        line = row[0]
        if enclosing_method and line_to_member.get(line) != enclosing_method:
            continue
        if enclosing_class and line_to_type.get(line) != enclosing_class:
            continue
        keep.append(row)
    return keep


def q_casts(src, tree, lines, type_name,
            enclosing_method=None, enclosing_class=None):
    results = []
    for node in _iter_cast_nodes(tree, src, type_name):
        if not _passes_enclosing_filter(node, src,
                                        enclosing_method, enclosing_class):
            continue
        row = node.start_point[0]
        snippet = lines[row].strip() if row < len(lines) else ""
        prefix = _scope_prefix(node, src)
        results.append((_line(node),
                        f"{prefix}{snippet}" if prefix else snippet))
    results.sort(key=lambda x: x[0])
    return results


def q_accesses_on(src, tree, lines, type_name,
                  enclosing_method=None, enclosing_class=None):
    var_map = _build_var_type_map(tree, src)

    def _matches(name: str, node) -> bool:
        t = var_map.resolve_at(name, node)
        return bool(t) and type_name in _type_names(t)

    results = []
    seen_rows = set()

    def _emit(node, member_name):
        if not _passes_enclosing_filter(node, src,
                                        enclosing_method, enclosing_class):
            return
        row = node.start_point[0]
        if row in seen_rows:
            return
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        prefix = _scope_prefix(node, src)
        results.append(
            (_line(node),
             f"{prefix}.{member_name}  <- {line_text}"
             if prefix else f".{member_name}  <- {line_text}"))

    # Direct member access: var.Member
    for node in _find_all(tree.root_node, lambda n: n.type == "member_access_expression"):
        if _in_literal(node):
            continue
        obj    = node.child_by_field_name("expression")
        member = node.child_by_field_name("name")
        if not obj or not member:
            continue
        if obj.type != "identifier":
            continue
        if not _matches(_text(obj, src).strip(), node):
            continue
        _emit(node, _text(member, src).strip())

    # Null-conditional member access: var?.Member
    for node in _find_all(tree.root_node, lambda n: n.type == "conditional_access_expression"):
        if _in_literal(node):
            continue
        cond = node.child_by_field_name("condition")
        if not cond or cond.type != "identifier":
            continue
        if not _matches(_text(cond, src).strip(), node):
            continue
        binding = next((c for c in node.children
                        if c.type == "member_binding_expression"), None)
        if binding:
            member = binding.child_by_field_name("name")
            if member:
                _emit(node, _text(member, src).strip())

    # Object-initializer member assignments -- new T { Prop = val }
    # Each assignment is emitted independently so multiple members on the same
    # line are all reported.
    for assign, type_node, lhs in _iter_initializer_members(tree, src):
        if not type_node or type_name not in _type_names(_text(type_node, src).strip()):
            continue
        if not _passes_enclosing_filter(assign, src,
                                        enclosing_method, enclosing_class):
            continue
        row = assign.start_point[0]
        line_text = lines[row].strip() if row < len(lines) else ""
        prefix = _scope_prefix(assign, src)
        body = f".{_text(lhs, src).strip()}  <- {line_text}"
        results.append((_line(assign), f"{prefix}{body}" if prefix else body))

    # With-expression member mutations (C# 9 records) -- obj with { Prop = val }
    # Each member is emitted independently for the same reason as above.
    for wi, src_ident, prop in _iter_with_members(tree, src):
        if not _matches(_text(src_ident, src).strip(), wi):
            continue
        if not _passes_enclosing_filter(wi, src,
                                        enclosing_method, enclosing_class):
            continue
        row = wi.start_point[0]
        line_text = lines[row].strip() if row < len(lines) else ""
        prefix = _scope_prefix(wi, src)
        body = f".{_text(prop, src).strip()}  <- {line_text}"
        results.append((_line(wi), f"{prefix}{body}" if prefix else body))

    results.sort(key=lambda x: x[0])
    return results


def q_all_refs(src, tree, lines, name,
               enclosing_method=None, enclosing_class=None):
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != name:
            continue
        if _in_literal(node):
            continue
        if not _passes_enclosing_filter(node, src,
                                        enclosing_method, enclosing_class):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def q_var_type(src, tree, lines, name):
    """Report the resolved type of every occurrence of ``name`` in the file.

    For each identifier-position where ``name`` appears, run the method-
    scoped var-type resolver and emit:

      L<line>: name : <ResolvedType>          when the resolver returns a type
      L<line>: name : (unresolved)            when the resolver returns None
      L<line>: name : (conflicting)           when the name is known but its
                                              scope had conflicting declarations

    Identical (line, resolved) pairs are deduped so a name used multiple
    times on the same line reports once. Identifiers inside string
    literals or comments are skipped -- matches mirror ``all_refs``.
    """
    var_map = _build_var_type_map(tree, src)
    results = []
    seen = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != name:
            continue
        if _in_literal(node):
            continue
        resolved = var_map.resolve_at(name, node)
        if resolved is None:
            # Distinguish "name known but ambiguous" (sentinel set during
            # construction) from "name never declared in scope". The map's
            # resolve_at returns None for both, so re-check membership.
            in_any = any(
                name in m for m in var_map._scope_maps.values()  # noqa: SLF001
            ) or name in var_map._file_map  # noqa: SLF001
            label = "(conflicting)" if in_any else "(unresolved)"
        else:
            label = resolved
        row = node.start_point[0]
        key = (row, label)
        if key in seen:
            continue
        seen.add(key)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), f"{name} : {label}  <- {line_text}"))
    return results


# -- Process function ----------------------------------------------------------

def query_cs_bytes(src_bytes: bytes, mode: str, mode_arg: str, include_body=False,
                   symbol_kind=None, uses_kind=None, visibility=None,
                   head_lines=None, enclosing_method=None,
                   enclosing_class=None, **kwargs):
    """Parse C# bytes and return list[{"line": N, "text": "..."}] for the given mode."""
    src_bytes = _strip_else_branches(src_bytes)
    try:
        tree = _cs_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing C# source: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: q_classes(src_bytes, tree, lines, visibility=visibility),
        "methods":      lambda: q_methods(src_bytes, tree, lines, visibility=visibility),
        "fields":       lambda: q_fields(src_bytes, tree, lines, visibility=visibility),
        "calls":        lambda: q_calls(src_bytes, tree, lines, mode_arg,
                                        enclosing_method=enclosing_method,
                                        enclosing_class=enclosing_class),
        "caller_of":    lambda: q_caller_of(src_bytes, tree, lines, mode_arg),
        "callee_of":    lambda: q_callee_of(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: q_implements(src_bytes, tree, lines, mode_arg),
        "uses":         lambda: q_uses(src_bytes, tree, lines, mode_arg, uses_kind=uses_kind,
                                       enclosing_method=enclosing_method,
                                       enclosing_class=enclosing_class),
        "accesses_on":  lambda: q_accesses_on(src_bytes, tree, lines, mode_arg,
                                              enclosing_method=enclosing_method,
                                              enclosing_class=enclosing_class),
        "all_refs":     lambda: q_all_refs(src_bytes, tree, lines, mode_arg,
                                           enclosing_method=enclosing_method,
                                           enclosing_class=enclosing_class),
        "casts":        lambda: q_casts(src_bytes, tree, lines, mode_arg,
                                        enclosing_method=enclosing_method,
                                        enclosing_class=enclosing_class),
        "attrs":        lambda: q_attrs(src_bytes, tree, lines, mode_arg),
        "accesses_of":  lambda: q_accesses_of(src_bytes, tree, lines, mode_arg,
                                              enclosing_method=enclosing_method,
                                              enclosing_class=enclosing_class),
        "imports":      lambda: q_usings(src_bytes, tree, lines),
        "declarations": lambda: q_declarations(src_bytes, tree, lines, mode_arg,
                                               include_body=include_body, symbol_kind=symbol_kind,
                                               visibility=visibility, head_lines=head_lines),
        "body":         lambda: q_body(src_bytes, tree, lines, mode_arg,
                                       symbol_kind=symbol_kind, head_lines=head_lines),
        "at":           lambda: q_at(src_bytes, tree, lines, mode_arg),
        "params":       lambda: q_params(src_bytes, tree, lines, mode_arg),
        "var_type":     lambda: q_var_type(src_bytes, tree, lines, mode_arg),
    }
    return _run_dispatch(mode, "C#", dispatch)


def describe_cs_file(src_bytes: bytes, ext: str = "") -> FileDescription:
    """Return all structured C# data from src_bytes as a FileDescription."""
    src_bytes = _strip_else_branches(src_bytes)
    try:
        tree = _cs_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing C# source: {e}", file=sys.stderr)
        return FileDescription(language="cs")

    # One tree walk shared by all extractors below. Buckets every type any
    # extractor needs and collects literal-aware all_refs in the same pass.
    idx = _CsIndex(src_bytes, tree, _DESCRIBE_NODE_TYPES, collect_refs=True)

    # Var-type map drives qualified-call resolution at index time. Built once
    # per file; consulted by the call-site emitter to attach a stable
    # ``Type.Method`` token to every receiver it can pin down syntactically.
    var_map = _build_var_type_map(tree, src_bytes)

    return FileDescription(
        language="cs",
        classes=_q_classes_data(src_bytes, idx),
        methods=_q_methods_data(src_bytes, idx),
        fields=_q_fields_data(src_bytes, idx),
        imports=_q_usings_data(src_bytes, idx),
        attrs=_q_attrs_data(src_bytes, idx),
        namespace=_q_namespace(src_bytes, idx),
        call_site_infos=_q_all_call_site_infos(src_bytes, idx, var_map),
        cast_infos=[CastInfo(target_type=t) for t in _q_all_cast_types_data(src_bytes, idx)],
        local_var_infos=[LocalVarInfo(var_type=t) for t in _q_all_local_types_data(src_bytes, idx)],
        member_access_infos=[MemberAccessInfo(member=m) for m in _q_all_member_accesses_data(src_bytes, idx)],
        all_refs=idx.all_refs,
    )
