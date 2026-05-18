"""
AST query dispatch -- routes query_file and describe_file to the right language module.

Each language module owns its parser, preprocessing, and query_*_bytes / describe_*_file.
"""

import re

from ._util import FileDescription  # noqa: F401  (re-exported for callers)


_GENERIC_IDENT_RE = re.compile(rb'[A-Za-z_][A-Za-z0-9_]*')

from .cs import (
    EXTENSIONS as CS_EXTENSIONS,
    query_cs_bytes, describe_cs_file,
)

from .py import (
    EXTENSIONS as PY_EXTENSIONS,
    query_py_bytes, describe_py_file,
)

from .rust import (
    EXTENSIONS as RUST_EXTENSIONS,
    query_rust_bytes, describe_rust_file,
)

from .js import (
    EXTENSIONS as JS_EXTENSIONS,
    query_js_bytes, describe_js_file,
)

from .cpp import (
    EXTENSIONS as CPP_EXTENSIONS,
    query_cpp_bytes, describe_cpp_file,
)

from .sql import query_sql_bytes, describe_sql_file


# -- Extension routing tables --------------------------------------------------

def _make_js_query(ext):
    """Return a query_bytes function that passes ext to query_js_bytes."""
    def _fn(src_bytes, mode, mode_arg, **kwargs):
        return query_js_bytes(src_bytes, mode, mode_arg, ext=ext, **kwargs)
    return _fn


_EXT_TO_QUERY_BYTES = {
    **{ext: query_cs_bytes   for ext in CS_EXTENSIONS},
    **{ext: query_py_bytes   for ext in PY_EXTENSIONS},
    **{ext: query_rust_bytes for ext in RUST_EXTENSIONS},
    **{ext: _make_js_query(ext) for ext in JS_EXTENSIONS},
    **{ext: query_cpp_bytes  for ext in CPP_EXTENSIONS},
    ".sql": query_sql_bytes,
}

_EXT_TO_DESCRIBER = {
    **{ext: describe_cs_file   for ext in CS_EXTENSIONS},
    **{ext: describe_py_file   for ext in PY_EXTENSIONS},
    **{ext: describe_rust_file for ext in RUST_EXTENSIONS},
    **{ext: describe_js_file   for ext in JS_EXTENSIONS},
    **{ext: describe_cpp_file  for ext in CPP_EXTENSIONS},
    ".sql": describe_sql_file,
}

ALL_EXTS = set(_EXT_TO_QUERY_BYTES.keys())


def query_file(src_bytes: bytes, ext: str, mode: str, mode_arg: str = "",
               include_body=False, symbol_kind=None, uses_kind=None,
               visibility=None, head_lines=None,
               enclosing_method=None, enclosing_class=None, **kwargs):
    """Query src_bytes using the given mode.

    Returns ``list[{"line": N, "text": "..."}]`` on success.

    Raises ``ValueError`` if the extension has no registered language or if
    the mode isn't supported for that language -- explicit errors beat silent
    empties for tool-using agents. Use ``mode='capabilities'`` to ask which
    modes a given file supports.

    ``visibility`` is an optional comma-separated filter for declaration
    modes (``classes``/``methods``/``fields``/``declarations``). Languages
    that don't capture visibility silently ignore it.

    ``head_lines`` truncates each ``body`` / ``declarations include_body=True``
    match to the first N source lines (signature + body together) with a
    ``... +K more lines`` tail marker. Other modes ignore it.

    ``enclosing_method`` / ``enclosing_class`` narrow pattern-mode hits to
    those that occur inside a member / type with the given name. Useful
    for call-site context queries like ``calls("Save",
    enclosing_method="WriteBack")``. Languages that don't capture
    enclosing-scope structure silently ignore these.
    """
    fn = _EXT_TO_QUERY_BYTES.get(ext)
    if fn is None:
        raise ValueError(
            f"extension {ext!r} has no registered language parser. "
            f"Supported extensions: {', '.join(sorted(ALL_EXTS))}"
        )
    return fn(src_bytes, mode, mode_arg,
              include_body=include_body, symbol_kind=symbol_kind,
              uses_kind=uses_kind, visibility=visibility,
              head_lines=head_lines,
              enclosing_method=enclosing_method,
              enclosing_class=enclosing_class, **kwargs)


def describe_file(src_bytes: bytes, ext: str) -> FileDescription:
    """Return all structured data from src_bytes as a FileDescription.

    Unknown extensions still get an identifier-bag pre-filter via a regex pass,
    so plain-text formats (markdown, configs, etc.) remain searchable by name.
    """
    fn = _EXT_TO_DESCRIBER.get(ext)
    if fn is None:
        bag = {m.decode("utf-8", "replace") for m in _GENERIC_IDENT_RE.findall(src_bytes)}
        return FileDescription(language="unknown", all_refs=bag)
    return fn(src_bytes, ext=ext)
