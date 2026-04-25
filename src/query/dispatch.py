"""
AST query dispatch — pure query layer, no CLI.

All process_*_file functions return list[{"line": N, "text": "..."}].
For the CLI entry point see indexserver/query_util.py.
"""

import os
import re
import sys

# ── C# preprocessor normaliser ────────────────────────────────────────────────

_PP_RE = re.compile(rb'^\s*#\s*(\w+)')

def _strip_else_branches(src_bytes: bytes) -> bytes:
    """Pre-process C# source for tree-sitter: assume all #if conditions are true.

    - Keeps code in #if / #ifdef / #ifndef branches.
    - Blanks out code in #else / #elif branches (replaces each line with a bare newline).
    - Replaces all preprocessor directive lines with bare newlines.

    Line count and therefore line numbers are preserved exactly, so AST
    start_point row values still correspond to the original file.
    """
    lines = src_bytes.splitlines(keepends=True)
    result: list[bytes] = []
    # Stack of booleans: True = we are currently inside a skipped (else) branch.
    skip_stack: list[bool] = []

    def _skipping() -> bool:
        return bool(skip_stack) and skip_stack[-1]

    for line in lines:
        m = _PP_RE.match(line)
        directive = m.group(1).lower() if m else b""

        if directive in (b"if", b"ifdef", b"ifndef"):
            # Enter a new if-block.  If already skipping (parent else branch),
            # the whole nested block is skipped too.
            skip_stack.append(_skipping())
            result.append(b"\n")
        elif directive in (b"elif", b"else"):
            # Switch from the if-branch (keep) to the else-branch (skip).
            if skip_stack:
                skip_stack[-1] = True
            result.append(b"\n")
        elif directive == b"endif":
            if skip_stack:
                skip_stack.pop()
            result.append(b"\n")
        elif directive:
            # Any other preprocessor directive (#pragma, #region, #nullable, …)
            result.append(b"\n")
        else:
            result.append(b"\n" if _skipping() else line)

    return b"".join(result)


# ── tree-sitter parsers ───────────────────────────────────────────────────────

from tree_sitter import Language, Parser

import tree_sitter_c_sharp as tscsharp
CS = Language(tscsharp.language())
_parser = Parser(CS)

from ..ast.cs import (
    _TYPE_DECL_NODES, _MEMBER_DECL_NODES, _QUALIFIED_RE,
    _find_all, _text, _unqualify, _unqualify_type,
    _base_type_names, _collect_ctor_names,
    SYMBOL_KIND_TO_NODES,
)

# ── C# query functions (imported from cs.py) ─────────────────────────────────

from .cs import (
    EXTENSIONS as CS_EXTENSIONS,
    q_classes, q_methods, q_fields, q_calls, q_accesses_of, q_implements,
    q_uses, q_attrs, q_usings, q_declarations, q_params, q_casts,
    q_accesses_on, q_all_refs,
    _line, _strip_generic, _type_names, _in_literal,
    _field_type, _build_sig, _enclosing_type_name,
    _q_uses_all, _q_field_type, _q_param_type, _q_return_type,
    _q_local_type, _q_base_uses,
)

import tree_sitter_python as tspython
_PY = Language(tspython.language())
_py_parser = Parser(_PY)

from .py import (
    EXTENSIONS as PY_EXTENSIONS,
    py_q_classes, py_q_methods, py_q_calls, py_q_implements, py_q_ident,
    py_q_declarations, py_q_decorators, py_q_imports, py_q_params,
    _py_in_literal, _py_enclosing_class, _py_base_names,
)

import tree_sitter_rust as tsrust
_RUST = Language(tsrust.language())
_rust_parser = Parser(_RUST)

from .rust import (
    EXTENSIONS as RUST_EXTENSIONS,
    rust_q_classes, rust_q_methods, rust_q_calls, rust_q_implements,
    rust_q_declarations, rust_q_all_refs, rust_q_imports, rust_q_params,
)

import tree_sitter_javascript as tsjs
_JS = Language(tsjs.language())
_js_parser = Parser(_JS)

import tree_sitter_typescript as tsts
_TS = Language(tsts.language_typescript())
_ts_parser = Parser(_TS)
_TSX = Language(tsts.language_tsx())
_tsx_parser = Parser(_TSX)

from .js import (
    EXTENSIONS as JS_EXTENSIONS,
    TS_EXTENSIONS as JS_TS_EXTENSIONS,
    TSX_EXTENSIONS as JS_TSX_EXTENSIONS,
    js_q_classes, js_q_methods, js_q_calls, js_q_implements,
    js_q_declarations, js_q_all_refs, js_q_imports, js_q_params, js_q_attrs,
)

import tree_sitter_cpp as tscpp
_CPP = Language(tscpp.language())
_cpp_parser = Parser(_CPP)

from .cpp import (
    EXTENSIONS as CPP_EXTENSIONS,
    cpp_q_classes, cpp_q_methods, cpp_q_calls, cpp_q_implements,
    cpp_q_declarations, cpp_q_all_refs, cpp_q_includes, cpp_q_params,
)

from .sql import (
    sql_q_text, sql_q_declarations, sql_q_fields, sql_q_calls,
    sql_q_classes, sql_q_methods,
)

# Availability flags checked by api.py to skip parsers whose grammar failed to load.
# All grammars are hard dependencies here (import errors would have already raised),
# so these are unconditionally True.
_PY_AVAILABLE   = True
_RUST_AVAILABLE = True
_JS_AVAILABLE   = True
_TS_AVAILABLE   = True
_CPP_AVAILABLE  = True


# ── Per-file process functions ────────────────────────────────────────────────
# These use module-level parsers for efficiency (api.py accesses _q._parser etc.)
#
# All process_*_file functions return list[{"line": N, "text": "..."}].
# Display (path prefix, count_only, context lines) is the caller's responsibility.

def _make_matches(results):
    """Convert raw (line_str, text) tuples from query functions to match dicts."""
    out = []
    for line_num_str, text in results:
        try:
            line_int = int(line_num_str)
        except (ValueError, TypeError):
            line_int = 0
        out.append({"line": line_int, "text": (text or "").rstrip()})
    return out


def process_cs_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Process a C# file. Returns list[{"line": N, "text": "..."}]."""
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    # Normalise preprocessor directives before handing to tree-sitter.
    # tree-sitter-c-sharp cannot evaluate #if conditions; directives that split
    # a syntactic construct (e.g. #else inside a property accessor) produce
    # cascading ERROR nodes that hide the rest of the file's declarations.
    # We assume every #if condition is true and blank out #else/#elif branches.
    src_bytes = _strip_else_branches(src_bytes)
    try:
        tree = _parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: q_classes(src_bytes, tree, lines),
        "methods":      lambda: q_methods(src_bytes, tree, lines),
        "fields":       lambda: q_fields(src_bytes, tree, lines),
        "calls":        lambda: q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: q_implements(src_bytes, tree, lines, mode_arg),
        "uses":         lambda: q_uses(src_bytes, tree, lines, mode_arg, uses_kind=uses_kind),
        "accesses_on":  lambda: q_accesses_on(src_bytes, tree, lines, mode_arg),
        "all_refs":     lambda: q_all_refs(src_bytes, tree, lines, mode_arg),
        "casts":        lambda: q_casts(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: q_attrs(src_bytes, tree, lines, mode_arg),
        "accesses_of":  lambda: q_accesses_of(src_bytes, tree, lines, mode_arg),
        "usings":       lambda: q_usings(src_bytes, tree, lines),
        "declarations": lambda: q_declarations(src_bytes, tree, lines, mode_arg,
                                               include_body=include_body, symbol_kind=symbol_kind),
        "params":       lambda: q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if fn is None:
        raise ValueError(f"Unknown mode: {mode!r}")
    return _make_matches(fn() or [])


def process_py_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Process a Python file. Returns list[{"line": N, "text": "..."}]."""
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: py_q_classes(src_bytes, tree, lines),
        "methods":      lambda: py_q_methods(src_bytes, tree, lines),
        "calls":        lambda: py_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: py_q_implements(src_bytes, tree, lines, mode_arg),
        "ident":        lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "all_refs":     lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: py_q_declarations(src_bytes, tree, lines, mode_arg),
        "decorators":   lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: py_q_imports(src_bytes, tree, lines),
        "params":       lambda: py_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode: {mode!r}", file=sys.stderr)
        return []
    return _make_matches(fn() or [])


def process_rust_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Process a Rust file. Returns list[{"line": N, "text": "..."}]."""
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = _rust_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: rust_q_classes(src_bytes, tree, lines),
        "methods":      lambda: rust_q_methods(src_bytes, tree, lines),
        "calls":        lambda: rust_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: rust_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: rust_q_declarations(src_bytes, tree, lines, mode_arg,
                                                    include_body=include_body),
        "all_refs":     lambda: rust_q_all_refs(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: rust_q_imports(src_bytes, tree, lines),
        "params":       lambda: rust_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode for Rust: {mode!r}", file=sys.stderr)
        return []
    return _make_matches(fn() or [])


def process_js_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Process a JS/TS file. Returns list[{"line": N, "text": "..."}]."""
    ext = os.path.splitext(path)[1].lower()
    if ext in JS_TS_EXTENSIONS:
        parser = _tsx_parser if ext in JS_TSX_EXTENSIONS else _ts_parser
    else:
        parser = _js_parser

    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: js_q_classes(src_bytes, tree, lines),
        "methods":      lambda: js_q_methods(src_bytes, tree, lines),
        "calls":        lambda: js_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: js_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: js_q_declarations(src_bytes, tree, lines, mode_arg,
                                                  include_body=include_body),
        "all_refs":     lambda: js_q_all_refs(src_bytes, tree, lines, mode_arg),
        "imports":      lambda: js_q_imports(src_bytes, tree, lines),
        "params":       lambda: js_q_params(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: js_q_attrs(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode for JS/TS: {mode!r}", file=sys.stderr)
        return []
    return _make_matches(fn() or [])


def process_cpp_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Process a C/C++ file. Returns list[{"line": N, "text": "..."}]."""
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
    try:
        tree = _cpp_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return []

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":      lambda: cpp_q_classes(src_bytes, tree, lines),
        "methods":      lambda: cpp_q_methods(src_bytes, tree, lines),
        "calls":        lambda: cpp_q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: cpp_q_implements(src_bytes, tree, lines, mode_arg),
        "declarations": lambda: cpp_q_declarations(src_bytes, tree, lines, mode_arg,
                                                   include_body=include_body),
        "all_refs":     lambda: cpp_q_all_refs(src_bytes, tree, lines, mode_arg),
        "includes":     lambda: cpp_q_includes(src_bytes, tree, lines),
        "params":       lambda: cpp_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode for C/C++: {mode!r}", file=sys.stderr)
        return []
    return _make_matches(fn() or [])


def process_sql_file(path, mode, mode_arg, include_body=False, **kwargs):
    """Process a SQL file. Returns list[{"line": N, "text": "..."}].
    Uses regex-based matching (T-SQL not fully supported by tree-sitter-sql)."""
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []

    text = src_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    dispatch = {
        "text":         lambda: sql_q_text(lines, mode_arg),
        "declarations": lambda: sql_q_declarations(text, lines, mode_arg),
        "fields":       lambda: sql_q_fields(text, lines, mode_arg),
        "calls":        lambda: sql_q_calls(text, lines, mode_arg),
        "classes":      lambda: sql_q_classes(text, lines),
        "methods":      lambda: sql_q_methods(text, lines),
    }

    fn = dispatch.get(mode)
    if not fn:
        # Fall back to text search for unsupported modes
        fn = lambda: sql_q_text(lines, mode_arg) if mode_arg else []
    return _make_matches(fn() or [])


# ── Extension → process function routing ─────────────────────────────────────
# Built from EXTENSIONS constants defined in each language module.

_EXT_TO_PROCESSOR = {
    **{ext: process_cs_file  for ext in CS_EXTENSIONS},
    **{ext: process_py_file  for ext in PY_EXTENSIONS},
    **{ext: process_rust_file for ext in RUST_EXTENSIONS},
    **{ext: process_js_file  for ext in JS_EXTENSIONS},
    **{ext: process_cpp_file for ext in CPP_EXTENSIONS},
    ".sql":  process_sql_file,
}

def process_any_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Dispatch to the correct language processor. Returns list[{"line": N, "text": "..."}]."""
    ext = os.path.splitext(path)[1].lower()
    fn = _EXT_TO_PROCESSOR.get(ext, process_cs_file)
    return fn(path, mode, mode_arg, include_body=include_body,
              symbol_kind=symbol_kind, uses_kind=uses_kind)


_ALL_EXTS = set(_EXT_TO_PROCESSOR.keys())
