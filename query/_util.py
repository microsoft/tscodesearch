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


@dataclass
class ClassInfo:
    """A type declaration (class, struct, interface, enum, trait, union, …)."""
    line: int
    name: str
    kind: str
    bases: list = dc_field(default_factory=list)

    @property
    def text(self) -> str:
        suffix = f" : {', '.join(self.bases)}" if self.bases else ""
        return f"[{self.kind}] {self.name}{suffix}"


@dataclass
class MethodInfo:
    """A function, method, constructor, property accessor, or event member."""
    line: int
    name: str
    kind: str
    sig: str = ""
    cls_name: str = ""
    return_type: str = ""
    param_types: list = dc_field(default_factory=list)

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
    """Convert (line, text) tuples from query functions to list[{"line": N, "text": "..."}]."""
    out = []
    for line_num, text in results:
        try:
            line_int = int(line_num)
        except (ValueError, TypeError):
            line_int = 0
        out.append({"line": line_int, "text": (text or "").rstrip()})
    return out
