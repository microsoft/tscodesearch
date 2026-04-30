from dataclasses import dataclass, field as dc_field


@dataclass
class FileDescription:
    """All structured data extracted from a source file in a single parse."""
    path: str
    language: str
    classes: list = dc_field(default_factory=list)
    methods: list = dc_field(default_factory=list)
    fields: list  = dc_field(default_factory=list)
    imports: list = dc_field(default_factory=list)
    attrs: list   = dc_field(default_factory=list)


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
