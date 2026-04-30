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


# ── C# ────────────────────────────────────────────────────────────────────────

@dataclass
class CsClassInfo:
    line: int
    name: str
    kind: str
    bases: list

    @property
    def text(self) -> str:
        suffix = f" : {', '.join(self.bases)}" if self.bases else ""
        return f"[{self.kind}] {self.name}{suffix}"


@dataclass
class CsMemberInfo:
    line: int
    name: str
    kind: str          # "method" | "ctor" | "field" | "prop" | "event"
    sig: str | None = None
    return_type: str | None = None
    param_types: list = dc_field(default_factory=list)
    field_type: str | None = None

    @property
    def text(self) -> str:
        label = f"[{self.kind}]".ljust(9)
        if self.kind in ("method", "ctor"):
            return f"{label}{self.sig}"
        return f"{label}{self.field_type} {self.name}".rstrip()


@dataclass
class CsFieldInfo:
    line: int
    name: str
    kind: str          # "field" | "prop"
    field_type: str

    @property
    def text(self) -> str:
        label = f"[{self.kind}]".ljust(8)
        return f"{label}{self.field_type} {self.name}".rstrip()


@dataclass
class CsUsingInfo:
    line: int
    text: str
    namespace: str


@dataclass
class CsAttrInfo:
    line: int
    text: str
    attr_name: str


# ── Python ────────────────────────────────────────────────────────────────────

@dataclass
class PyClassInfo:
    line: int
    name: str
    bases: list

    @property
    def text(self) -> str:
        suffix = f"({', '.join(self.bases)})" if self.bases else ""
        return f"[class] {self.name}{suffix}"


@dataclass
class PyMethodInfo:
    line: int
    name: str
    kind: str          # "def" | "method"
    params_str: str
    cls_name: str = ""
    return_type: str | None = None
    param_types: list = dc_field(default_factory=list)

    @property
    def sig(self) -> str:
        ret = f" -> {self.return_type}" if self.return_type else ""
        return f"def {self.name}{self.params_str}{ret}"

    @property
    def text(self) -> str:
        prefix = f"[in {self.cls_name}] " if self.cls_name else ""
        ret = f" -> {self.return_type}" if self.return_type else ""
        return f"[{self.kind}] {prefix}{self.name}{self.params_str}{ret}"


@dataclass
class PyAttrInfo:
    line: int
    text: str
    attr_name: str


@dataclass
class PyImportInfo:
    line: int
    text: str
    module: str


# ── JavaScript / TypeScript ───────────────────────────────────────────────────

@dataclass
class JsClassInfo:
    line: int
    name: str
    kind: str
    bases: list

    @property
    def text(self) -> str:
        suffix = f" : {', '.join(self.bases)}" if self.bases else ""
        return f"[{self.kind}] {self.name}{suffix}"


@dataclass
class JsMethodInfo:
    line: int
    name: str
    kind: str          # "function" | "method"
    sig: str
    cls_name: str = ""

    @property
    def text(self) -> str:
        prefix = f"[in {self.cls_name}] " if self.cls_name else ""
        return f"[{self.kind}] {prefix}{self.sig}"


@dataclass
class JsImportInfo:
    line: int
    text: str
    module: str


# ── Rust ──────────────────────────────────────────────────────────────────────

@dataclass
class RustClassInfo:
    line: int
    name: str
    kind: str

    @property
    def text(self) -> str:
        return f"[{self.kind}] {self.name}"


@dataclass
class RustMethodInfo:
    line: int
    name: str
    kind: str
    sig: str
    impl_type: str = ""

    @property
    def text(self) -> str:
        prefix = f"[in {self.impl_type}] " if self.impl_type else ""
        return f"[{self.kind}] {prefix}{self.sig}"


# ── C / C++ ───────────────────────────────────────────────────────────────────

@dataclass
class CppClassInfo:
    line: int
    name: str
    kind: str
    bases: list

    @property
    def text(self) -> str:
        suffix = f" : {', '.join(self.bases)}" if self.bases else ""
        return f"[{self.kind}] {self.name}{suffix}"


@dataclass
class CppMethodInfo:
    line: int
    name: str
    kind: str          # "function" | "method"
    sig: str
    cls_name: str = ""

    @property
    def text(self) -> str:
        prefix = f"[in {self.cls_name}] " if self.cls_name else ""
        return f"[{self.kind}] {prefix}{self.sig}"


# ── Shared ────────────────────────────────────────────────────────────────────

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
