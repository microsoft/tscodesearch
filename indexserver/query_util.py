"""
Structural AST query CLI for the indexserver -- supports C#, Python, Rust, JavaScript, TypeScript, C/C++.

Extends query/dispatch.py with Tantivy-backed file discovery (--search), filesystem
glob expansion, and display helpers (expand_files, print_file_matches).

Usage:
    python -m indexserver.query_util MODE [OPTIONS] FILE [FILE ...] [GLOB_PATTERN ...]

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
    --attrs           [NAME]  List [Attribute] / @decorator / #[attribute] usages
    --imports              List all using / import / include directives
    --declarations     NAME   Print declaration(s) named NAME
    --params           METHOD Show the full parameter list of METHOD

Modes (Python / Rust / JS / TS / C++):
    --classes / --methods / --calls / --implements / --declarations
    --all-refs / --imports / --params
    Python / TypeScript also support: --attrs

Options:
    --no-path              Don't prefix output with file path
    --count                Print only match counts per file + total
    --json                 Output results as JSON
"""

import os
import sys
import argparse
import json as _json

# Add the tscodesearch root to sys.path so query/indexserver are importable.
_ts_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ts_root not in sys.path:
    sys.path.insert(0, _ts_root)

from query.dispatch import query_file, ALL_EXTS
from indexserver.config import normalize_path


# -- Filesystem helpers --------------------------------------------------------

def expand_files(patterns, exts=None):
    import glob as _glob
    if exts is None:
        exts = {".cs"}
    files = []
    seen  = set()
    for pat in patterns:
        pat = normalize_path(pat)
        if any(c in pat for c in ("*", "?")):
            for f in sorted(_glob.glob(pat, recursive=True)):
                f = normalize_path(f)
                ext = os.path.splitext(f)[1].lower()
                if ext in exts and f not in seen:
                    seen.add(f)
                    files.append(f)
        elif os.path.isdir(pat):
            for root, _, fnames in os.walk(pat):
                for fn in sorted(fnames):
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in exts:
                        fp = normalize_path(os.path.join(root, fn))
                        if fp not in seen:
                            seen.add(fp)
                            files.append(fp)
        elif os.path.isfile(pat) and pat not in seen:
            seen.add(pat)
            files.append(pat)
    return files


def print_file_matches(matches, disp, show_path, count_only, context, mode, path):
    """Print matches for one file to stdout. Returns match count."""
    if not matches:
        return 0
    if count_only:
        print(f"{len(matches):4d}  {disp}")
        return len(matches)
    lines = None
    if context > 0 and mode != "declarations":
        try:
            with open(path, "rb") as _f:
                lines = _f.read().decode("utf-8", errors="replace").splitlines()
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


# -- Tantivy file resolver -----------------------------------------------------

def files_from_search(query, sub=None, ext="cs", limit=50,
                      collection=None, src_root=None, query_by=None,
                      exclude_path=None):
    """Run a backend search and return the local file paths of matching documents."""
    from indexserver.config import load_config as _load_config
    from indexserver.indexer import ensure_backend
    from indexserver.search import search as _backend_search

    _cfg = _load_config()
    coll_name = collection or _cfg.collection
    src_root_native = normalize_path(src_root or _cfg.src_root)

    filter_parts = [f"extension:={ext.lstrip('.')}"] if ext else []
    if sub:
        filter_parts.append(f"path_segments:={sub.replace(chr(92), '/').strip('/')}")
    if exclude_path:
        excluded = [p.replace(chr(92), '/').strip('/') for p in exclude_path.split(",")]
        excluded = [p for p in excluded if p]
        if len(excluded) == 1:
            filter_parts.append(f"path_segments:!={excluded[0]}")
        elif excluded:
            filter_parts.append(f"path_segments:!=[{','.join(excluded)}]")
    filter_by = " && ".join(filter_parts) if filter_parts else ""

    try:
        backend_cm = ensure_backend(_cfg, coll_name, write=False)
    except Exception as e:
        print(f"Index open error: {e}", file=sys.stderr)
        print("Run: ts recreate", file=sys.stderr)
        return []

    with backend_cm as backend:
        result = _backend_search(
            backend,
            q=query,
            query_by=query_by or "filename,class_names,method_names,tokens",
            per_page=limit,
            num_typos=1,
            filter_by=filter_by,
        )

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
    print(f"[search] '{query}' -> {found} index hits, {len(paths)} local files",
          file=sys.stderr)
    return paths


# -- CLI -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mg = ap.add_mutually_exclusive_group(required=True)
    mg.add_argument("--classes",      action="store_true")
    mg.add_argument("--methods",      action="store_true")
    mg.add_argument("--fields",       action="store_true")
    mg.add_argument("--calls",        metavar="METHOD")
    mg.add_argument("--implements",   metavar="TYPE")
    mg.add_argument("--uses",         metavar="TYPE")
    mg.add_argument("--casts",        metavar="TYPE")
    mg.add_argument("--all-refs",     metavar="NAME")
    mg.add_argument("--accesses-of",  metavar="MEMBER")
    mg.add_argument("--attrs",        metavar="NAME", nargs="?", const="")
    mg.add_argument("--declarations", metavar="NAME")
    mg.add_argument("--params",       metavar="METHOD")
    mg.add_argument("--imports",      action="store_true")

    ap.add_argument("files", nargs="*", metavar="FILE_OR_PATTERN")
    ap.add_argument("--search",       metavar="QUERY")
    ap.add_argument("--search-sub",   metavar="FOLDER_PATH")
    ap.add_argument("--search-exclude-path", metavar="FOLDERS",
                    help="Exclude files under these folders (comma-separated)")
    ap.add_argument("--search-ext",   metavar="EXT", default="cs")
    ap.add_argument("--search-limit", metavar="N", type=int, default=50)
    ap.add_argument("--uses-kind",    metavar="KIND", default="")
    ap.add_argument("--symbol-kind",  metavar="KIND", default="")
    ap.add_argument("--include-body", action="store_true")
    ap.add_argument("--no-path",      action="store_true")
    ap.add_argument("--count",        action="store_true")
    ap.add_argument("--context",      metavar="N", type=int, default=0)
    ap.add_argument("--json",         action="store_true",
                    help='Output results as JSON: {"results": [{"file": ..., "matches": [{"line": N, "text": ...}]}]}')
    args = ap.parse_args()

    if not args.files and not args.search:
        ap.error("Provide FILE_OR_PATTERN arguments or use --search QUERY")

    if args.classes:
        mode, mode_arg = "classes",      None
    elif args.methods:
        mode, mode_arg = "methods",      None
    elif args.fields:
        mode, mode_arg = "fields",       None
    elif args.calls:
        mode, mode_arg = "calls",        args.calls
    elif args.implements:
        mode, mode_arg = "implements",   args.implements
    elif args.uses:
        mode, mode_arg = "uses",         args.uses
    elif args.casts:
        mode, mode_arg = "casts",        args.casts
    elif args.all_refs:
        mode, mode_arg = "all_refs",     args.all_refs
    elif args.accesses_of:
        mode, mode_arg = "accesses_of",  args.accesses_of
    elif args.attrs is not None:
        mode, mode_arg = "attrs",        args.attrs or None
    elif args.declarations:
        mode, mode_arg = "declarations", args.declarations
    elif args.params:
        mode, mode_arg = "params",       args.params
    elif args.imports:
        mode, mode_arg = "imports",      None
    else:
        ap.print_help(); sys.exit(1)

    if args.search:
        files = files_from_search(
            query=args.search,
            sub=getattr(args, "search_sub", None),
            ext=getattr(args, "search_ext", "cs"),
            limit=getattr(args, "search_limit", 50),
            exclude_path=getattr(args, "search_exclude_path", None),
        )
        if not files:
            print("No matching files found in index.", file=sys.stderr)
            sys.exit(1)
    else:
        files = expand_files(args.files, exts=ALL_EXTS)
        if not files:
            print(f"No supported files found: {' '.join(args.files)}", file=sys.stderr)
            sys.exit(1)

    has_glob     = any(c in p for p in (args.files or []) for c in ("*", "?"))
    show_path    = not args.no_path and (len(files) > 1 or has_glob or bool(args.search))
    uses_kind    = getattr(args, "uses_kind", "") or ""
    symbol_kind  = getattr(args, "symbol_kind", "") or ""
    include_body = getattr(args, "include_body", False)
    context      = args.context

    def _query(f):
        ext = os.path.splitext(f)[1].lower()
        try:
            with open(f, "rb") as _fh:
                src_bytes = _fh.read()
        except OSError as e:
            print(f"ERROR reading {f}: {e}", file=sys.stderr)
            return []
        return query_file(src_bytes, ext, mode, mode_arg,
                          include_body=include_body,
                          symbol_kind=symbol_kind,
                          uses_kind=uses_kind)

    if args.json:
        all_results = []
        for f in files:
            matches = _query(f)
            if matches:
                all_results.append({"file": f, "matches": matches})
        print(_json.dumps({"results": all_results}))
    else:
        total = 0
        for f in files:
            matches = _query(f)
            disp    = normalize_path(f)
            total  += print_file_matches(matches, disp, show_path, args.count,
                                         context, mode, f)
        if args.count:
            print(f"\nTotal: {total}")
        elif len(files) > 1:
            print(f"\n({total} matches across {len(files)} files)", file=sys.stderr)


if __name__ == "__main__":
    main()
