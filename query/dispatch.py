"""
AST query dispatch — routes query_file and describe_file to the right language module.

Each language module owns its parser, preprocessing, and query_*_bytes / describe_*_file.
"""

import sys

from ._util import FileDescription  # noqa: F401  (re-exported for callers)

from .cs import (
    EXTENSIONS as CS_EXTENSIONS,
    _cs_parser, _strip_else_branches,
    query_cs_bytes, describe_cs_file,
    _q_classes_data, _q_methods_data, _q_fields_data, _q_usings_data, _q_attrs_data,
    q_methods, q_fields, q_classes, q_calls, q_implements, q_uses, q_casts,
    q_all_refs, q_accesses_on, q_accesses_of, q_attrs, q_usings, q_declarations, q_params,
)

from .py import (
    EXTENSIONS as PY_EXTENSIONS,
    _py_parser,
    query_py_bytes, describe_py_file,
    _py_q_classes_data, _py_q_methods_data, _py_q_attrs_data, _py_q_imports_data,
)

from .rust import (
    EXTENSIONS as RUST_EXTENSIONS,
    _rust_parser,
    query_rust_bytes, describe_rust_file,
    _rust_q_classes_data, _rust_q_methods_data,
)

from .js import (
    EXTENSIONS as JS_EXTENSIONS,
    TS_EXTENSIONS as JS_TS_EXTENSIONS,
    TSX_EXTENSIONS as JS_TSX_EXTENSIONS,
    _js_parser, _ts_parser, _tsx_parser,
    query_js_bytes, describe_js_file,
    _js_q_classes_data, _js_q_methods_data, _js_q_imports_data,
)

from .cpp import (
    EXTENSIONS as CPP_EXTENSIONS,
    _cpp_parser,
    query_cpp_bytes, describe_cpp_file,
    _cpp_q_classes_data, _cpp_q_methods_data,
)

from .sql import query_sql_bytes, describe_sql_file


# ── Extension routing tables ──────────────────────────────────────────────────

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

_ALL_EXTS = set(_EXT_TO_QUERY_BYTES.keys())


def query_file(src_bytes: bytes, ext: str, mode: str, mode_arg: str = "",
               include_body=False, symbol_kind=None, uses_kind=None, **kwargs):
    """Query src_bytes using the given mode. Returns list[{"line": N, "text": "..."}]."""
    fn = _EXT_TO_QUERY_BYTES.get(ext)
    if fn is None:
        return []
    return fn(src_bytes, mode, mode_arg,
              include_body=include_body, symbol_kind=symbol_kind, uses_kind=uses_kind, **kwargs)


def describe_file(src_bytes: bytes, ext: str) -> FileDescription:
    """Return all structured data from src_bytes as a FileDescription."""
    fn = _EXT_TO_DESCRIBER.get(ext)
    if fn is None:
        return FileDescription(language="unknown")
    return fn(src_bytes, ext=ext)
