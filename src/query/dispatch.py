"""
Structural AST query tool — supports C#, Python, Rust, JavaScript, TypeScript, C/C++.

Use instead of grep when you need semantically precise searches that understand
syntax: distinguishes type references from method calls, skips comments and
string literals, understands inheritance hierarchies.

Usage:
    query.py MODE [OPTIONS] FILE [FILE ...] [GLOB_PATTERN ...]

Modes (C#):
    --classes              List all type declarations with their base types
    --methods              List all method/constructor/property/field signatures
    --fields               List all field and property declarations with types
    --calls    METHOD      Find every call site of METHOD
    --implements TYPE      Find type declarations that inherit or implement TYPE
    --uses     TYPE        Find every place TYPE is referenced as a type
    --casts    TYPE        Find every explicit cast expression (TYPE)expr
    --all-refs         NAME   Find every identifier occurrence
    --accesses-of      MEMBER Find every access site of property/field MEMBER
    --attrs           [NAME]  List [Attribute] decorators, optionally filter by NAME
    --usings               List all using/using-alias directives
    --declarations     NAME   Print declaration(s) named NAME
    --params           METHOD Show the full parameter list of METHOD

Modes (Python / Rust / JS / TS / C++):
    --classes / --methods / --calls / --implements / --declarations
    --all-refs / --imports / --params
    TypeScript also supports: --attrs (decorators)
    C/C++ also supports: --includes

Options:
    --no-path              Don't prefix output with file path
    --count                Print only match counts per file + total
"""

import os
import re
import sys
import glob as _glob
import argparse
import json as _json
import urllib.request
import urllib.parse

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


# ── Typesense file resolver ───────────────────────────────────────────────────

def _ts_search(collection: str, params: dict) -> dict:
    from .config import HOST, PORT, API_KEY
    qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{qs}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json.loads(r.read())


def files_from_search(query, sub=None, ext="cs", limit=50,
                      collection=None, src_root=None, query_by=None):
    """Run a Typesense search and return the local file paths of matching documents."""
    from .config import COLLECTION, SRC_ROOT, to_native_path
    coll_name = collection or COLLECTION
    root = src_root or SRC_ROOT
    src_root_native = to_native_path(root)

    filter_parts = [f"extension:={ext.lstrip('.')}"] if ext else []
    if sub:
        filter_parts.append(f"subsystem:={sub}")

    params = {
        "q":         query,
        "query_by":  query_by or "filename,symbols,class_names,method_names,content",
        "per_page":  limit,
        "prefix":    "false",
        "num_typos": "1",
    }
    if filter_parts:
        params["filter_by"] = " && ".join(filter_parts)

    try:
        result = _ts_search(coll_name, params)
    except Exception as e:
        print(f"Typesense search error: {e}", file=sys.stderr)
        print("Is the server running? Try: ts start", file=sys.stderr)
        return []

    paths = []
    seen  = set()
    for hit in result.get("hits", []):
        doc = hit["document"]
        rel = doc.get("relative_path", "")
        if not rel:
            continue
        path = os.path.join(src_root_native, rel.replace("/", os.sep))
        if path not in seen and os.path.isfile(path):
            seen.add(path)
            paths.append(path)

    found = result.get("found", len(paths))
    print(f"[search] '{query}' → {found} index hits, {len(paths)} local files",
          file=sys.stderr)
    return paths


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


# ── Glob expansion ────────────────────────────────────────────────────────────

_ALL_EXTS = set(_EXT_TO_PROCESSOR.keys())

def expand_files(patterns, exts=None):
    if exts is None:
        exts = {".cs"}  # backward compat: default to C# only
    files = []
    seen  = set()
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if any(c in pat for c in ("*", "?")):
            for f in sorted(_glob.glob(pat, recursive=True)):
                f = f.replace("\\", "/")
                ext = os.path.splitext(f)[1].lower()
                if ext in exts and f not in seen:
                    seen.add(f)
                    files.append(f)
        elif os.path.isdir(pat):
            for root, _, fnames in os.walk(pat):
                for fn in sorted(fnames):
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in exts:
                        fp = os.path.join(root, fn).replace("\\", "/")
                        if fp not in seen:
                            seen.add(fp)
                            files.append(fp)
        elif os.path.isfile(pat) and pat not in seen:
            seen.add(pat)
            files.append(pat)
    return files


# ── CLI display helpers ───────────────────────────────────────────────────────

def _print_file_matches(matches, disp, show_path, count_only, context, mode, path):
    """Print matches for one file to stdout. Returns match count."""
    if not matches:
        return 0
    if count_only:
        print(f"{len(matches):4d}  {disp}")
        return len(matches)
    lines = None
    if context > 0 and mode != "declarations":
        try:
            lines = open(path, "rb").read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            pass
    for m in matches:
        ln, text = m["line"], m["text"]
        print(f"{disp}:{ln}: {text}" if show_path else f"{ln}: {text}")
        if context > 0 and mode != "declarations" and lines is not None:
            try:
                row   = ln - 1
                start = max(0, row - context)
                end   = min(len(lines), row + context + 1)
                for i, cl in enumerate(lines[start:end], start):
                    if i == row:
                        continue
                    prefix = f"  {disp}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {cl}")
                print()
            except (ValueError, IndexError):
                pass
    return len(matches)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mg = ap.add_mutually_exclusive_group(required=True)
    mg.add_argument("--classes",    action="store_true")
    mg.add_argument("--methods",    action="store_true")
    mg.add_argument("--fields",     action="store_true")
    mg.add_argument("--calls",      metavar="METHOD")
    mg.add_argument("--implements", metavar="TYPE")
    mg.add_argument("--uses",       metavar="TYPE")
    mg.add_argument("--casts",      metavar="TYPE")
    mg.add_argument("--all-refs",         metavar="NAME")
    mg.add_argument("--accesses-of",      metavar="MEMBER")
    mg.add_argument("--attrs",            metavar="NAME", nargs="?", const="")
    mg.add_argument("--usings",     action="store_true")
    mg.add_argument("--declarations", metavar="NAME")
    mg.add_argument("--params",     metavar="METHOD")
    mg.add_argument("--imports",    action="store_true")
    mg.add_argument("--includes",   action="store_true")

    ap.add_argument("files", nargs="*", metavar="FILE_OR_PATTERN")
    ap.add_argument("--search",       metavar="QUERY")
    ap.add_argument("--search-sub",   metavar="SUBSYSTEM")
    ap.add_argument("--search-ext",   metavar="EXT", default="cs")
    ap.add_argument("--search-limit", metavar="N", type=int, default=50)
    ap.add_argument("--uses-kind", metavar="KIND", default="")
    ap.add_argument("--no-path", action="store_true")
    ap.add_argument("--count",   action="store_true")
    ap.add_argument("--context", metavar="N", type=int, default=0)
    ap.add_argument("--json",    action="store_true",
                    help="Output results as JSON: {\"results\": [{\"file\": ..., \"matches\": [{\"line\": N, \"text\": ...}]}]}")
    args = ap.parse_args()

    if not args.files and not args.search:
        ap.error("Provide FILE_OR_PATTERN arguments or use --search QUERY")

    if args.classes:
        mode, mode_arg = "classes",    None
    elif args.methods:
        mode, mode_arg = "methods",    None
    elif args.fields:
        mode, mode_arg = "fields",     None
    elif args.calls:
        mode, mode_arg = "calls",      args.calls
    elif args.implements:
        mode, mode_arg = "implements", args.implements
    elif args.uses:
        mode, mode_arg = "uses",       args.uses
    elif args.casts:
        mode, mode_arg = "casts",      args.casts
    elif args.all_refs:
        mode, mode_arg = "all_refs",   args.all_refs
    elif args.accesses_of:
        mode, mode_arg = "accesses_of", args.accesses_of
    elif args.attrs is not None:
        mode, mode_arg = "attrs",      args.attrs or None
    elif args.usings:
        mode, mode_arg = "usings",     None
    elif args.declarations:
        mode, mode_arg = "declarations", args.declarations
    elif args.params:
        mode, mode_arg = "params",     args.params
    elif args.imports:
        mode, mode_arg = "imports",    None
    elif args.includes:
        mode, mode_arg = "includes",   None
    else:
        ap.print_help(); sys.exit(1)

    if args.search:
        files = files_from_search(
            query=args.search,
            sub=getattr(args, "search_sub", None),
            ext=getattr(args, "search_ext", "cs"),
            limit=getattr(args, "search_limit", 50),
        )
        if not files:
            print("No matching files found in index.", file=sys.stderr)
            sys.exit(1)
    else:
        files = expand_files(args.files, exts=_ALL_EXTS)
        if not files:
            print(f"No supported files found: {' '.join(args.files)}", file=sys.stderr)
            sys.exit(1)

    has_glob  = any(c in p for p in (args.files or []) for c in ("*", "?"))
    show_path = not args.no_path and (len(files) > 1 or has_glob or bool(args.search))
    uses_kind = getattr(args, "uses_kind", "") or ""
    context   = args.context

    if args.json:
        all_results = []
        for f in files:
            matches = process_any_file(f, mode, mode_arg, uses_kind=uses_kind)
            if matches:
                all_results.append({"file": f, "matches": matches})
        print(_json.dumps({"results": all_results}))
    else:
        total = 0
        for f in files:
            matches  = process_any_file(f, mode, mode_arg, uses_kind=uses_kind)
            disp     = f.replace("\\", "/")
            total   += _print_file_matches(matches, disp, show_path, args.count,
                                           context, mode, f)
        if args.count:
            print(f"\nTotal: {total}")
        elif len(files) > 1:
            print(f"\n({total} matches across {len(files)} files)", file=sys.stderr)


if __name__ == "__main__":
    main()
