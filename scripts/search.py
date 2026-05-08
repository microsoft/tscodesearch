"""
Search the local Tantivy code index.

Usage:
    search.py "query" [--ext cs] [--sub myservice] [--limit 10] [--symbols] [--json]

Modes:
    Default:         full-text search across filenames, declared names, and tokens
    --symbols:       search only declared names (class/interface/method)
    --implements X:  find types that inherit from or implement X
    --calls X:       find call sites that invoke method X
    --uses X:        find files that reference type X in declarations
    --attr X:        find files decorated with attribute X
    --ext EXT:       filter by file extension (e.g. cs, h, py)
    --sub PATH:      filter by ancestor folder (e.g. myservice, services/billing)
    --exclude-path P: exclude files under any of these folders (comma-separated)
    --limit N:       max results (default 10)

Examples:
    search.py "IStorageProvider"
    search.py "GetItemsAsync" --ext cs --sub myservice
    search.py "WriteItemsAsync" --symbols
    search.py "circuit breaker" --sub core --limit 5
    search.py "IStorageProvider" --implements
    search.py "GetItemsAsync" --calls
    search.py "ItemInfo" --uses
    search.py "Obsolete" --attr
"""

import os
import sys
import json
import argparse

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from indexserver.config import load_config as _load_config
from indexserver.indexer import ensure_backend
from indexserver.search import search as _backend_search
from query.cs import symbol_kind_query_by

_cfg = _load_config()


def search(query, ext=None, sub=None, limit=10,
           symbols_only=False, implements=False, calls=False,
           uses=False, attrs=False, casts=False, accesses_of=False, collection=None,
           symbol_kind=None, uses_kind="", exclude_path=None):
    coll_name = collection or _cfg.collection

    # Determine query_by based on mode
    if implements:
        query_by = "base_types,class_names,filename"
    elif calls:
        query_by = "call_sites,filename"
    elif uses:
        k = (uses_kind or "all").lower().strip()
        if k == "field":
            query_by = "field_types,filename"
        elif k == "param":
            query_by = "param_types,filename"
        elif k == "return":
            query_by = "return_types,filename"
        elif k == "cast":
            query_by = "cast_types,filename"
        elif k == "base":
            query_by = "base_types,class_names,filename"
        elif k == "locals":
            query_by = "local_types,filename"
        else:
            query_by = "type_refs,cast_types,filename"
    elif attrs:
        query_by = "attr_names,filename"
    elif casts:
        query_by = "cast_types,filename"
    elif accesses_of:
        query_by = "member_accesses,filename"
    elif symbols_only:
        narrowed = symbol_kind_query_by(symbol_kind or "")
        query_by = narrowed if narrowed else "class_names,method_names,filename"
    else:
        query_by = "filename,class_names,method_names,tokens"

    # When a C/C++ source extension is requested, automatically include C/C++ headers.
    _CPP_SRC = {"cpp", "cc", "cxx", "c"}
    _CPP_HDR = {"h", "hpp", "hxx"}
    filter_parts = []
    if ext:
        exts = {e.lstrip(".") for e in ext.split(",")}
        if exts & _CPP_SRC:
            exts |= _CPP_HDR
        if len(exts) == 1:
            filter_parts.append(f"extension:={next(iter(exts))}")
        else:
            filter_parts.append(f"extension:=[{','.join(sorted(exts))}]")
    if sub:
        included = [p.replace(chr(92), '/').strip('/') for p in sub.split(",")]
        included = [p for p in included if p]
        if len(included) == 1:
            filter_parts.append(f"path_segments:={included[0]}")
        elif included:
            filter_parts.append(f"path_segments:=[{','.join(included)}]")
    if exclude_path:
        excluded = [p.replace(chr(92), '/').strip('/') for p in exclude_path.split(",")]
        excluded = [p for p in excluded if p]
        if len(excluded) == 1:
            filter_parts.append(f"path_segments:!={excluded[0]}")
        elif excluded:
            filter_parts.append(f"path_segments:!=[{','.join(excluded)}]")
    filter_by = " && ".join(filter_parts) if filter_parts else ""

    try:
        backend = ensure_backend(_cfg, coll_name, write=False)
    except Exception as e:
        print(f"ERROR: cannot open index for collection '{coll_name}': {e}")
        print(f"  Run: ts recreate")
        sys.exit(1)

    try:
        result = _backend_search(
            backend,
            q=query,
            query_by=query_by,
            per_page=limit,
            num_typos=1,
            filter_by=filter_by,
            facet_by="path_segments,language,extension",
            max_facet_values=200,
        )
    except Exception as e:
        print(f"ERROR: search failed: {e}")
        sys.exit(1)
    finally:
        backend.close()

    return result, query_by


def format_results(result, query, query_by, show_facets=False, debug=False):
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

    for i, hit in enumerate(hits, 1):
        doc = hit["document"]
        rel = doc.get("relative_path", "")
        ns = doc.get("namespace", "")

        print(f"{i}. {rel}")

        class_names  = doc.get("class_names",  []) or []
        method_names = doc.get("method_names", []) or []
        base_types   = doc.get("base_types",   []) or []
        member_sigs  = doc.get("member_sigs",  []) or []
        attr_names   = doc.get("attr_names",   []) or []
        usings       = doc.get("usings",       []) or []

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
        if usings:     print(f"   Usings     : {', '.join(usings[:4])}")
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
        format_results(result, args.query, query_by, show_facets=args.facets, debug=args.debug)


if __name__ == "__main__":
    main()
