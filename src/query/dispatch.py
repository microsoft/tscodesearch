"""
AST query dispatch — pure query layer, no CLI.

All process_*_file functions return list[{"line": N, "text": "..."}].
For the CLI entry point see indexserver/query_util.py.
"""

import os
import sys

# ── base tree-sitter (required) ───────────────────────────────────────────────

from tree_sitter import Language, Parser

# ── C# parser (optional) ──────────────────────────────────────────────────────

try:
    import tree_sitter_c_sharp as tscsharp
    _CS_AVAILABLE = True
    CS = Language(tscsharp.language())
    _parser = Parser(CS)
except ImportError:
    _CS_AVAILABLE = False
    tscsharp = None
    _parser = None

from ..ast.cs import (
    _TYPE_DECL_NODES, _MEMBER_DECL_NODES, _QUALIFIED_RE,
    _find_all, _text, _unqualify, _unqualify_type,
    _base_type_names, _collect_ctor_names,
    SYMBOL_KIND_TO_NODES,
)

# ── C# query functions (imported from cs.py) ─────────────────────────────────

from .cs import (
    q_classes, q_methods, q_fields, q_calls, q_accesses_of, q_implements,
    q_uses, q_attrs, q_usings, q_declarations, q_params, q_casts,
    q_accesses_on, q_all_refs,
    _line, _strip_generic, _type_names, _in_literal,
    _field_type, _build_sig, _enclosing_type_name,
    _q_uses_all, _q_field_type, _q_param_type, _q_return_type,
    _q_local_type, _q_base_uses,
)

# ── Python parser (optional) ──────────────────────────────────────────────────

try:
    import tree_sitter_python as tspython
    _PY_AVAILABLE = True
    _PY = Language(tspython.language())
    _py_parser = Parser(_PY)
except ImportError:
    _PY_AVAILABLE = False
    tspython = None
    _py_parser = None

from .py import (
    py_q_classes, py_q_methods, py_q_calls, py_q_implements, py_q_ident,
    py_q_declarations, py_q_decorators, py_q_imports, py_q_params,
    _py_in_literal, _py_enclosing_class, _py_base_names,
)

# ── Rust parser (optional) ────────────────────────────────────────────────────

try:
    import tree_sitter_rust as tsrust
    _RUST_AVAILABLE = True
    _RUST = Language(tsrust.language())
    _rust_parser = Parser(_RUST)
except ImportError:
    _RUST_AVAILABLE = False
    _rust_parser = None

from .rust import (
    rust_q_classes, rust_q_methods, rust_q_calls, rust_q_implements,
    rust_q_declarations, rust_q_all_refs, rust_q_imports, rust_q_params,
)

# ── JavaScript parser (optional) ─────────────────────────────────────────────

try:
    import tree_sitter_javascript as tsjs
    _JS_AVAILABLE = True
    _JS = Language(tsjs.language())
    _js_parser = Parser(_JS)
except ImportError:
    _JS_AVAILABLE = False
    _js_parser = None

# ── TypeScript parsers (optional) ─────────────────────────────────────────────

try:
    import tree_sitter_typescript as tsts
    _TS_AVAILABLE = True
    _TS = Language(tsts.language_typescript())
    _ts_parser = Parser(_TS)
    _TSX = Language(tsts.language_tsx())
    _tsx_parser = Parser(_TSX)
except ImportError:
    _TS_AVAILABLE = False
    _ts_parser = None
    _tsx_parser = None

from .js import (
    js_q_classes, js_q_methods, js_q_calls, js_q_implements,
    js_q_declarations, js_q_all_refs, js_q_imports, js_q_params, js_q_attrs,
)

# ── C/C++ parser (optional) ───────────────────────────────────────────────────

try:
    import tree_sitter_cpp as tscpp
    _CPP_AVAILABLE = True
    _CPP = Language(tscpp.language())
    _cpp_parser = Parser(_CPP)
except ImportError:
    _CPP_AVAILABLE = False
    _cpp_parser = None

from .cpp import (
    cpp_q_classes, cpp_q_methods, cpp_q_calls, cpp_q_implements,
    cpp_q_declarations, cpp_q_all_refs, cpp_q_includes, cpp_q_params,
)


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
    if not _CS_AVAILABLE or _parser is None:
        print("ERROR: tree-sitter and tree-sitter-c-sharp are required. "
              "Run: pip install tree-sitter tree-sitter-c-sharp", file=sys.stderr)
        return []
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return []
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
    return _make_matches(fn() or []) if fn else []


def process_py_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Process a Python file. Returns list[{"line": N, "text": "..."}]."""
    if not _PY_AVAILABLE or _py_parser is None:
        print("ERROR: tree-sitter-python not installed. "
              "Run: pip install tree-sitter-python", file=sys.stderr)
        return []
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
    if not _RUST_AVAILABLE or _rust_parser is None:
        print("ERROR: tree-sitter-rust not installed. "
              "Run: pip install tree-sitter-rust", file=sys.stderr)
        return []
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
    if ext in (".ts", ".tsx"):
        if not _TS_AVAILABLE:
            print("ERROR: tree-sitter-typescript not installed. "
                  "Run: pip install tree-sitter-typescript", file=sys.stderr)
            return []
        parser = _tsx_parser if ext == ".tsx" else _ts_parser
    else:
        if not _JS_AVAILABLE:
            print("ERROR: tree-sitter-javascript not installed. "
                  "Run: pip install tree-sitter-javascript", file=sys.stderr)
            return []
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
    if not _CPP_AVAILABLE or _cpp_parser is None:
        print("ERROR: tree-sitter-cpp not installed. "
              "Run: pip install tree-sitter-cpp", file=sys.stderr)
        return []
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


# ── Extension → process function routing ─────────────────────────────────────

_EXT_TO_PROCESSOR = {
    ".cs":   process_cs_file,
    ".py":   process_py_file,
    ".rs":   process_rust_file,
    ".js":   process_js_file,  ".jsx":  process_js_file,
    ".mjs":  process_js_file,  ".cjs":  process_js_file,
    ".ts":   process_js_file,  ".tsx":  process_js_file,
    ".cpp":  process_cpp_file, ".cc":   process_cpp_file,
    ".cxx":  process_cpp_file, ".c":    process_cpp_file,
    ".hpp":  process_cpp_file, ".h":    process_cpp_file,
    ".hxx":  process_cpp_file,
}

def process_any_file(path, mode, mode_arg, include_body=False, symbol_kind=None, uses_kind=None):
    """Dispatch to the correct language processor. Returns list[{"line": N, "text": "..."}]."""
    ext = os.path.splitext(path)[1].lower()
    fn = _EXT_TO_PROCESSOR.get(ext, process_cs_file)
    return fn(path, mode, mode_arg, include_body=include_body,
              symbol_kind=symbol_kind, uses_kind=uses_kind)


_ALL_EXTS = set(_EXT_TO_PROCESSOR.keys())
