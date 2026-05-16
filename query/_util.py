from dataclasses import dataclass, field as dc_field


def _dedupe(seq) -> list:
    """Return seq with duplicates removed, preserving order, dropping falsy values."""
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


@dataclass
class CallSiteInfo:
    """A function or method call site."""
    name: str
    receiver: str = ""  # receiver identifier when it looks like a type (e.g. "Repo" in Repo.Save())


@dataclass
class CastInfo:
    """An explicit type cast."""
    target_type: str


@dataclass
class LocalVarInfo:
    """A local variable declaration."""
    var_type: str


@dataclass
class MemberAccessInfo:
    """A non-call member access (property or field read)."""
    member: str


@dataclass
class FileDescription:
    """All structured data extracted from source bytes in a single parse."""
    language: str

    # ── Declarations (query layer) ────────────────────────────────────────────
    classes:             list = dc_field(default_factory=list)  # list[ClassInfo]
    methods:             list = dc_field(default_factory=list)  # list[MethodInfo]
    fields:              list = dc_field(default_factory=list)  # list[FieldInfo]
    imports:             list = dc_field(default_factory=list)  # list[ImportInfo]
    attrs:               list = dc_field(default_factory=list)  # list[AttrInfo]

    # ── Code elements (indexer derives flat fields via flat_from_fd) ──────────
    namespace:           str  = ""
    call_site_infos:     list = dc_field(default_factory=list)  # list[CallSiteInfo]
    cast_infos:          list = dc_field(default_factory=list)  # list[CastInfo]
    local_var_infos:     list = dc_field(default_factory=list)  # list[LocalVarInfo]
    member_access_infos: list = dc_field(default_factory=list)  # list[MemberAccessInfo]

    # ── Identifier bag (drives the broad `tokens` pre-filter field) ───────────
    # Deduped identifier texts excluding tokens inside literal nodes.
    all_refs:            set  = dc_field(default_factory=set)


@dataclass
class ClassInfo:
    """A type declaration (class, struct, interface, enum, trait, union, …).

    ``line`` is the 1-indexed start line of the declaration; ``end_line`` is
    the 1-indexed last line of its body, inclusive. ``end_line == line`` for
    forward declarations / interface stubs that have no body.
    """
    line: int
    name: str
    kind: str
    bases: list = dc_field(default_factory=list)
    end_line: int = 0

    @property
    def text(self) -> str:
        suffix = f" : {', '.join(self.bases)}" if self.bases else ""
        return f"[{self.kind}] {self.name}{suffix}"


@dataclass
class MethodInfo:
    """A function, method, constructor, property accessor, or event member.

    ``line``/``end_line`` are 1-indexed and span the whole declaration
    including any leading attributes/decorators and the body. For fields
    (no body) ``end_line == line``.
    """
    line: int
    name: str
    kind: str
    sig: str = ""
    cls_name: str = ""
    return_type: str = ""
    param_types: list = dc_field(default_factory=list)
    # Every identifier-like token appearing in the member's signature —
    # attribute names, modifiers' identifiers, generic args, parameter names,
    # default-value identifiers, etc. Excludes the body. Populated by each
    # language's extractor using language-aware AST traversal; empty list
    # means the language doesn't yet emit them.
    sig_tokens: list = dc_field(default_factory=list)
    end_line: int = 0

    @property
    def text(self) -> str:
        prefix = f"[in {self.cls_name}] " if self.cls_name else ""
        return f"[{self.kind}] {prefix}{self.sig}".rstrip()


@dataclass
class FieldInfo:
    """A field or property declaration."""
    line: int
    name: str
    kind: str
    field_type: str = ""
    sig: str = ""
    # Same purpose as MethodInfo.sig_tokens — every identifier in the
    # field/property declaration excluding any initialiser body.
    sig_tokens: list = dc_field(default_factory=list)
    end_line: int = 0

    @property
    def text(self) -> str:
        return f"[{self.kind}] {self.field_type} {self.name}".rstrip()


@dataclass
class ImportInfo:
    """An import, using directive, or use declaration."""
    line: int
    text: str
    module: str = ""


@dataclass
class AttrInfo:
    """A decorator or attribute annotation."""
    line: int
    text: str
    attr_name: str = ""


def _make_matches(results):
    """Convert tuples from query functions to list of match dicts.

    Accepts either:
      * ``(line, text)`` — pattern-mode results; emits ``{"line": N, "text": ...}``
      * ``(line, end_line, text)`` — listing-mode results with scope; emits
        ``{"line": N, "end_line": M, "text": ...}``. ``end_line`` lets callers
        ``Read(file, offset=line, limit=end_line - line + 1)`` precisely.
    """
    out = []
    for row in results:
        if len(row) == 3:
            line_num, end_num, text = row
            try:    end_int = int(end_num)
            except (ValueError, TypeError):
                end_int = 0
        else:
            line_num, text = row
            end_int = 0
        try:
            line_int = int(line_num)
        except (ValueError, TypeError):
            line_int = 0
        match = {"line": line_int, "text": (text or "").rstrip()}
        if end_int and end_int != line_int:
            match["end_line"] = end_int
        out.append(match)
    return out


def node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


class TreeIndex:
    """Pre-computed buckets of AST nodes from a single tree-sitter walk.

    Single shared implementation used by every language module. The walk uses
    tree-sitter's TreeCursor (C-level traversal) to avoid materialising Python
    lists of children at each level — about 2× faster than a Python stack of
    nodes.

    Args:
        src: source bytes (used only when collecting all_refs).
        tree: tree-sitter Tree object.
        wanted: set of node-type strings to bucket; nodes outside this set are
            still visited but not stored. Pass a small set for one-off queries
            and the full describe-set for describe_*_file.
        literal_nodes: set of node-type strings whose subtrees count as
            "inside a literal" (strings, comments, regex, etc.). Identifiers
            whose nearest enclosing literal ancestor is not None are excluded
            from all_refs. Pass None (default) to skip literal-depth tracking.
        identifier_types: node-type strings that should be added to all_refs.
            Pass () to skip; only consulted when literal_nodes is also given.
    """

    __slots__ = ("nodes_by_type", "all_refs")

    def __init__(self, src: bytes, tree, wanted,
                 literal_nodes=None, identifier_types=()):
        nodes_by_type: dict[str, list] = {t: [] for t in wanted}
        all_refs: set[str] = set()
        cursor = tree.walk()

        if literal_nodes is not None and identifier_types:
            ident_set = (identifier_types if isinstance(identifier_types, (set, frozenset))
                         else frozenset(identifier_types))
            lit_depth_stack = [0]
            visited_children = False
            while True:
                if not visited_children:
                    node = cursor.node
                    nt = node.type
                    lit_depth = lit_depth_stack[-1]
                    if nt in wanted:
                        nodes_by_type[nt].append(node)
                    if lit_depth == 0 and nt in ident_set:
                        all_refs.add(node_text(node, src))
                    if cursor.goto_first_child():
                        lit_depth_stack.append(
                            lit_depth + (1 if nt in literal_nodes else 0)
                        )
                        continue
                    visited_children = True
                if cursor.goto_next_sibling():
                    visited_children = False
                elif not cursor.goto_parent():
                    break
                else:
                    lit_depth_stack.pop()
        else:
            visited_children = False
            while True:
                if not visited_children:
                    nt = cursor.node.type
                    if nt in wanted:
                        nodes_by_type[nt].append(cursor.node)
                    if cursor.goto_first_child():
                        continue
                    visited_children = True
                if cursor.goto_next_sibling():
                    visited_children = False
                elif not cursor.goto_parent():
                    break

        self.nodes_by_type = nodes_by_type
        self.all_refs = all_refs

    def of(self, *types):
        """Return all nodes whose type is in `types`, in document order."""
        if len(types) == 1:
            return self.nodes_by_type.get(types[0], [])
        out = []
        for t in types:
            out.extend(self.nodes_by_type.get(t, []))
        out.sort(key=lambda n: n.start_byte)
        return out
