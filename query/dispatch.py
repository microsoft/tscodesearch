"""
AST query dispatch — routes process_any_file and describe_file to the right language module.

Each language module owns its parser, preprocessing, and process_*_file / describe_*_file.
"""

import os
import sys

from ._util import FileDescription  # noqa: F401  (re-exported for callers)

from .cs import (
    EXTENSIONS as CS_EXTENSIONS,
    _cs_parser, _strip_else_branches,
    process_cs_file, describe_cs_file,
    _q_classes_data, _q_methods_data, _q_fields_data, _q_usings_data, _q_attrs_data,
)

from .py import (
    EXTENSIONS as PY_EXTENSIONS,
    _py_parser,
    process_py_file, describe_py_file,
    _py_q_classes_data, _py_q_methods_data, _py_q_attrs_data, _py_q_imports_data,
)

from .rust import (
    EXTENSIONS as RUST_EXTENSIONS,
    _rust_parser,
    process_rust_file, describe_rust_file,
    _rust_q_classes_data, _rust_q_methods_data,
)

from .js import (
    EXTENSIONS as JS_EXTENSIONS,
    TS_EXTENSIONS as JS_TS_EXTENSIONS,
    TSX_EXTENSIONS as JS_TSX_EXTENSIONS,
    _js_parser, _ts_parser, _tsx_parser,
    process_js_file, describe_js_file,
    _js_q_classes_data, _js_q_methods_data, _js_q_imports_data,
)

from .cpp import (
    EXTENSIONS as CPP_EXTENSIONS,
    _cpp_parser,
    process_cpp_file, describe_cpp_file,
    _cpp_q_classes_data, _cpp_q_methods_data,
)

from .sql import process_sql_file, describe_sql_file


# ── Extension → process function routing ─────────────────────────────────────

_EXT_TO_PROCESSOR = {
    **{ext: process_cs_file   for ext in CS_EXTENSIONS},
    **{ext: process_py_file   for ext in PY_EXTENSIONS},
    **{ext: process_rust_file for ext in RUST_EXTENSIONS},
    **{ext: process_js_file   for ext in JS_EXTENSIONS},
    **{ext: process_cpp_file  for ext in CPP_EXTENSIONS},
    ".sql": process_sql_file,
}

_EXT_TO_DESCRIBER = {
    **{ext: describe_cs_file   for ext in CS_EXTENSIONS},
    **{ext: describe_py_file   for ext in PY_EXTENSIONS},
    **{ext: describe_rust_file for ext in RUST_EXTENSIONS},
    **{ext: describe_js_file   for ext in JS_EXTENSIONS},
    **{ext: describe_cpp_file  for ext in CPP_EXTENSIONS},
    ".sql": describe_sql_file,
}

_ALL_EXTS = set(_EXT_TO_PROCESSOR.keys())


def process_any_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Dispatch to the correct language processor. Returns list[{"line": N, "text": "..."}]."""
    ext = os.path.splitext(path)[1].lower()
    fn = _EXT_TO_PROCESSOR.get(ext, process_cs_file)
    return fn(path, mode, mode_arg, include_body=include_body,
              symbol_kind=symbol_kind, uses_kind=uses_kind)


def describe_file(path: str) -> FileDescription:
    """Parse path once and return all structured data as a FileDescription."""
    ext = os.path.splitext(path)[1].lower()
    fn = _EXT_TO_DESCRIBER.get(ext)
    if fn is None:
        return FileDescription(path=path, language="unknown")
    return fn(path)
