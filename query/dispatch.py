"""
AST query dispatch — routes process_any_file and describe_file to the right language module.

Each language module owns its parser, preprocessing, and process_*_file implementation.
"""

import os
import sys
from dataclasses import dataclass, field as dc_field

from .cs import (
    EXTENSIONS as CS_EXTENSIONS,
    _cs_parser, _strip_else_branches,
    process_cs_file,
    _q_classes_data, _q_methods_data, _q_fields_data, _q_usings_data, _q_attrs_data,
)

from .py import (
    EXTENSIONS as PY_EXTENSIONS,
    _py_parser,
    process_py_file,
    _py_q_classes_data, _py_q_methods_data, _py_q_attrs_data, _py_q_imports_data,
)

from .rust import (
    EXTENSIONS as RUST_EXTENSIONS,
    _rust_parser,
    process_rust_file,
    _rust_q_classes_data, _rust_q_methods_data,
)

from .js import (
    EXTENSIONS as JS_EXTENSIONS,
    TS_EXTENSIONS as JS_TS_EXTENSIONS,
    TSX_EXTENSIONS as JS_TSX_EXTENSIONS,
    _js_parser, _ts_parser, _tsx_parser,
    process_js_file,
    _js_q_classes_data, _js_q_methods_data, _js_q_imports_data,
)

from .cpp import (
    EXTENSIONS as CPP_EXTENSIONS,
    _cpp_parser,
    process_cpp_file,
    _cpp_q_classes_data, _cpp_q_methods_data,
)

from .sql import process_sql_file


# ── Extension → process function routing ─────────────────────────────────────

_EXT_TO_PROCESSOR = {
    **{ext: process_cs_file   for ext in CS_EXTENSIONS},
    **{ext: process_py_file   for ext in PY_EXTENSIONS},
    **{ext: process_rust_file for ext in RUST_EXTENSIONS},
    **{ext: process_js_file   for ext in JS_EXTENSIONS},
    **{ext: process_cpp_file  for ext in CPP_EXTENSIONS},
    ".sql": process_sql_file,
}

_ALL_EXTS = set(_EXT_TO_PROCESSOR.keys())


def process_any_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Dispatch to the correct language processor. Returns list[{"line": N, "text": "..."}]."""
    ext = os.path.splitext(path)[1].lower()
    fn = _EXT_TO_PROCESSOR.get(ext, process_cs_file)
    return fn(path, mode, mode_arg, include_body=include_body,
              symbol_kind=symbol_kind, uses_kind=uses_kind)


# ── FileDescription ───────────────────────────────────────────────────────────

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


def describe_file(path: str) -> FileDescription:
    """Parse path once and return all structured data as a FileDescription."""
    ext = os.path.splitext(path)[1].lower()

    try:
        with open(path, "rb") as f:
            src_bytes = f.read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return FileDescription(path=path, language="unknown")

    if ext in CS_EXTENSIONS:
        src_bytes = _strip_else_branches(src_bytes)
        try:
            tree = _cs_parser.parse(src_bytes)
        except Exception as e:
            print(f"ERROR parsing {path}: {e}", file=sys.stderr)
            return FileDescription(path=path, language="cs")
        return FileDescription(
            path=path, language="cs",
            classes=_q_classes_data(src_bytes, tree),
            methods=_q_methods_data(src_bytes, tree),
            fields=_q_fields_data(src_bytes, tree),
            imports=_q_usings_data(src_bytes, tree),
            attrs=_q_attrs_data(src_bytes, tree),
        )

    if ext in PY_EXTENSIONS:
        try:
            tree = _py_parser.parse(src_bytes)
        except Exception as e:
            print(f"ERROR parsing {path}: {e}", file=sys.stderr)
            return FileDescription(path=path, language="py")
        return FileDescription(
            path=path, language="py",
            classes=_py_q_classes_data(src_bytes, tree),
            methods=_py_q_methods_data(src_bytes, tree),
            imports=_py_q_imports_data(src_bytes, tree),
            attrs=_py_q_attrs_data(src_bytes, tree),
        )

    if ext in JS_EXTENSIONS:
        parser = _tsx_parser if ext in JS_TSX_EXTENSIONS else (
            _ts_parser if ext in JS_TS_EXTENSIONS else _js_parser)
        try:
            tree = parser.parse(src_bytes)
        except Exception as e:
            print(f"ERROR parsing {path}: {e}", file=sys.stderr)
            return FileDescription(path=path, language="js")
        return FileDescription(
            path=path, language="js",
            classes=_js_q_classes_data(src_bytes, tree),
            methods=_js_q_methods_data(src_bytes, tree),
            imports=_js_q_imports_data(src_bytes, tree),
        )

    if ext in RUST_EXTENSIONS:
        try:
            tree = _rust_parser.parse(src_bytes)
        except Exception as e:
            print(f"ERROR parsing {path}: {e}", file=sys.stderr)
            return FileDescription(path=path, language="rust")
        return FileDescription(
            path=path, language="rust",
            classes=_rust_q_classes_data(src_bytes, tree),
            methods=_rust_q_methods_data(src_bytes, tree),
        )

    if ext in CPP_EXTENSIONS:
        try:
            tree = _cpp_parser.parse(src_bytes)
        except Exception as e:
            print(f"ERROR parsing {path}: {e}", file=sys.stderr)
            return FileDescription(path=path, language="cpp")
        return FileDescription(
            path=path, language="cpp",
            classes=_cpp_q_classes_data(src_bytes, tree),
            methods=_cpp_q_methods_data(src_bytes, tree),
        )

    return FileDescription(path=path, language="unknown")
