"""
Search the local Tantivy code index (read-only -- no daemon required).

Mode and filter resolution share their logic with the daemon via
``indexserver.search_modes`` -- adding a new mode there reaches both call sites.

Usage:
    search.py "query" [--ext cs] [--sub myservice] [--limit 10] [--symbols] [--json]

Modes:
    Default:         broad identifier search (filename + class/method names + tokens)
    --symbols:       restrict to declared class/interface/method names
    --implements:    types that inherit from or implement the queried type
    --calls:         call sites that invoke the queried method
    --uses:          files that reference the queried type
    --attrs:         files decorated with the queried attribute
    --casts:         explicit ``(T)expr`` / ``as T`` cast sites
    --ext EXT:       filter by file extension (e.g. cs, h, py).
                     Any C/C++ source extension auto-includes .h/.hpp/.hxx.
    --sub PATH:      restrict to an ancestor folder (e.g. myservice, services/billing)
    --exclude-path P: exclude files under any of these folders (comma-separated)
    --limit N:       max results (default 10)
    --facets:        also print folder/extension breakdowns
    --debug:         show full signature list per result
    --json:          dump the raw result dict

Examples:
    search.py "Widget"
    search.py "SaveChanges" --ext cs --sub services
    search.py "SaveChanges" --symbols
    search.py "IRepository" --implements
    search.py "SaveChanges" --calls
    search.py "Order" --uses
    search.py "Obsolete" --attrs
"""

import os
import sys
import json
import argparse

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from query.config import load_config as _load_config
from indexserver.indexer import ensure_backend
from indexserver.search import search as _backend_search
from indexserver.search_modes import build_filter_by, resolve_query_params

_cfg = _load_config()


def _ts_mode_flag(symbols_only, implements, calls, uses, attrs, casts, accesses_of) -> str:
    """Map this CLI's boolean flags to the ts_mode_flag the resolver expects."""
    if implements:  return "implements"
    if calls:       return "calls"
    if uses:        return "uses"
    if attrs:       return "attrs"
    if casts:       return "casts"
    if accesses_of: return "accesses_of"
    if symbols_only: return "symbols"
    return "all_refs"


def search(query, ext=None, sub=None, limit=10,
           symbols_only=False, implements=False, calls=False,
           uses=False, attrs=False, casts=False, accesses_of=False, collection=None,
           symbol_kind=None, uses_kind="", exclude_path=None):
    coll_name = collection or _cfg.collection

    mode_flag         = _ts_mode_flag(symbols_only, implements, calls, uses,
                                      attrs, casts, accesses_of)
    query_by, weights = resolve_query_params(mode_flag, uses_kind or "",
                                             symbol_kind or "")
    filter_by         = build_filter_by(ext or "", sub or "", exclude_path or "")

    try:
        backend_cm = ensure_backend(_cfg, coll_name, write=False)
    except Exception as e:
        print(f"ERROR: cannot open index for collection '{coll_name}': {e}")
        print(f"  Run: ts recreate")
        sys.exit(1)

    with backend_cm as backend:
        try:
            result = _backend_search(
                backend,
                q=query,
                query_by=query_by,
                weights=weights,
                per_page=limit,
                num_typos=1,
                filter_by=filter_by,
                facet_by="path_segments,language,extension",
                max_facet_values=200,
            )
        except Exception as e:
            print(f"ERROR: search failed: {e}")
            sys.exit(1)

    return result, query_by


def _root_for_collection(collection: str):
    """Return the configured Root whose collection name matches, or None."""
    for r in _cfg.roots.values():
        if r.collection == collection:
            return r
    return None


def _extract_for_display(rel_path: str, root) -> dict:
    """Re-parse a hit's source file to get class_names / base_types /
    member_sigs / etc. The schema no longer stores those (they're indexed
    but not retrievable) -- for a debug CLI display we re-extract on the fly.
    Returns an empty dict if the file can't be read."""
    if root is None or not rel_path:
        return {}
    import os
    from indexserver.indexer import extract_metadata
    full = os.path.join(root.path, rel_path)
    try:
        with open(full, "rb") as fh:
            return extract_metadata(fh.read(), os.path.splitext(full)[1].lower())
    except OSError:
        return {}


def format_results(result, query, query_by, show_facets=False, debug=False,
                   collection: str = ""):
    hits = result.get("hits", [])
    total = result.get("found", 0)

    print(f"=== Search: \"{query}\" ({total} results) [fields: {query_by}] ===\n")

    if show_facets or (total > 0 and not hits):
        for fc in result.get("facet_counts", []):
            field = fc["field_name"]
            counts = fc.get("counts", [])
            if counts:
                parts = ", ".join(f"{c['value']}({c['count']})" for c in counts[:15])
                print(f"  [{field}] {parts}")
        print()

    if not hits:
        print("No results found.")
        return

    root = _root_for_collection(collection)

    for i, hit in enumerate(hits, 1):
        doc = hit["document"]
        rel = doc.get("relative_path", "")
        meta = _extract_for_display(rel, root)
        ns = meta.get("namespace", "")
        if isinstance(ns, list):
            ns = ".".join(ns)

        print(f"{i}. {rel}")

        class_names  = meta.get("class_names",  []) or []
        method_names = meta.get("method_names", []) or []
        base_types   = meta.get("base_types",   []) or []
        member_sigs  = meta.get("member_sigs",  []) or []
        attr_names   = meta.get("attr_names",   []) or []
        imports      = meta.get("imports",      []) or []

        if class_names:  print(f"   Classes    : {', '.join(class_names[:5])}")
        if base_types:   print(f"   Implements : {', '.join(base_types[:5])}")
        if member_sigs:
            if debug:
                print(f"   Signatures ({len(member_sigs)}):")
                for s in member_sigs:
                    s = s.encode("ascii", errors="replace").decode("ascii")
                    print(f"     {s}")
            else:
                print(f"   Signatures : {'; '.join(member_sigs[:3])}")
        elif method_names:
            print(f"   Members    : {', '.join(method_names[:6])}")
        if attr_names: print(f"   Attributes : {', '.join(attr_names[:5])}")
        if imports:    print(f"   Imports    : {', '.join(imports[:4])}")
        if ns:         print(f"   NS         : {ns}")
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="Search query")
    ap.add_argument("--ext",    help="Filter by extension (e.g. cs, h, py)")
    ap.add_argument("--sub",    help="Filter by ancestor folder path (e.g. myservice or services/billing)")
    ap.add_argument("--exclude-path", help="Exclude files under any of these folders (comma-separated)")
    ap.add_argument("--limit",  type=int, default=10, help="Max results (default 10)")
    ap.add_argument("--symbols", action="store_true",
                    help="Search only symbol names")
    ap.add_argument("--implements", action="store_true",
                    help="Find types implementing/inheriting the query")
    ap.add_argument("--calls", action="store_true",
                    help="Find files that call the queried method")
    ap.add_argument("--uses", action="store_true",
                    help="Find files that reference the queried type")
    ap.add_argument("--attrs", action="store_true",
                    help="Find files decorated with the queried attribute")
    ap.add_argument("--casts", action="store_true",
                    help="Find files with explicit casts to the queried type")
    ap.add_argument("--facets", action="store_true",
                    help="Show folder/extension facet counts in output")
    ap.add_argument("--debug", action="store_true",
                    help="Show full signature list per result")
    ap.add_argument("--json", action="store_true",
                    help="Output raw JSON")
    args = ap.parse_args()

    result, query_by = search(
        query=args.query,
        ext=args.ext,
        sub=args.sub,
        limit=args.limit,
        symbols_only=args.symbols,
        implements=args.implements,
        calls=args.calls,
        uses=args.uses,
        attrs=args.attrs,
        casts=args.casts,
        exclude_path=args.exclude_path,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        format_results(result, args.query, query_by,
                       show_facets=args.facets, debug=args.debug,
                       collection=_cfg.collection)


if __name__ == "__main__":
    main()
