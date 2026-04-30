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


def _make_matches(results):
    """Convert (line_str, text) tuples from query functions to list[{"line": N, "text": "..."}]."""
    out = []
    for line_num_str, text in results:
        try:
            line_int = int(line_num_str)
        except (ValueError, TypeError):
            line_int = 0
        out.append({"line": line_int, "text": (text or "").rstrip()})
    return out
