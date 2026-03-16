"""
Search the Typesense code index.

Usage:
    search.py "query" [--ext cs] [--sub myservice] [--limit 10] [--symbols] [--json]

Modes:
    Default:         full-text search across filenames, symbols, and content
    --symbols:       search only C# symbol names (class/interface/method)
    --implements X:  find types that inherit from or implement X            [T1]
    --calls X:       find call sites that invoke method X                   [T1]
    --sig X:         find methods whose signature contains X                [T1]
    --uses X:        find files that reference type X in declarations       [T2]
    --attr X:        find files decorated with attribute X                  [T2]
    --ext EXT:       filter by file extension (e.g. cs, h, py)
    --sub NAME:      filter by subsystem (e.g. myservice, core, myapp)
    --limit N:       max results (default 10)

Examples:
    search.py "IStorageProvider"
    search.py "GetItemsAsync" --ext cs --sub myservice
    search.py "WriteItemsAsync" --symbols
    search.py "circuit breaker" --sub core --limit 5
    search.py "IStorageProvider" --implements     # find implementors
    search.py "GetItemsAsync" --calls             # find call sites
    search.py "Task GetItemsAsync" --sig          # find by signature
    search.py "ItemInfo" --uses                   # find type references
    search.py "Obsolete" --attr                   # find by attribute
"""


import os
import sys
import json
import argparse
import urllib.request
import urllib.parse

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)

from config import HOST, PORT, API_KEY, COLLECTION


def _ts_search(collection: str, params: dict) -> dict:
    """Send a search request to Typesense over HTTP (no typesense package needed)."""
    qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{qs}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def search(query, ext=None, sub=None, limit=10,
           symbols_only=False, implements=False, calls=False,
           sig=False, uses=False, attrs=False, casts=False, collection=None):
    from config import COLLECTION as _DEFAULT_COLLECTION
    coll_name = collection or _DEFAULT_COLLECTION

    # Determine query_by based on mode
    if implements:
        query_by = "base_types,class_names,filename"
    elif calls:
        query_by = "call_sites,filename"
    elif sig:
        query_by = "method_sigs,filename"
    elif uses:
        query_by = "type_refs,symbols,class_names,filename"
    elif attrs:
        query_by = "attributes,filename"
    elif casts:
        query_by = "cast_sites,filename"
    elif symbols_only:
        query_by = "symbols,class_names,method_names,filename"
    else:
        query_by = "filename,symbols,class_names,method_names,content"

    filter_parts = []
    if ext:
        filter_parts.append(f"extension:={ext.lstrip('.')}")
    if sub:
        filter_parts.append(f"subsystem:={sub}")
    filter_by = " && ".join(filter_parts) if filter_parts else ""

    params = {
        "q": query,
        "query_by": query_by,
        "per_page": limit,
        "highlight_full_fields": "symbols,class_names,method_names,filename,base_types,method_sigs",
        "snippet_threshold": 30,
        "num_typos": "1",
        "prefix": "false",
    }
    if filter_by:
        params["filter_by"] = filter_by

    params["facet_by"]  = "subsystem,extension"
    # Prefer .cs files (priority=3) when no explicit extension filter is set
    if not ext:
        params["sort_by"] = "_text_match:desc,priority:desc"

    try:
        result = _ts_search(coll_name, params)
    except Exception as e:
        msg = str(e)
        if "400" in msg or "non-indexed" in msg or "index" in msg.lower():
            print(f"ERROR: Schema issue - re-index with: ts index --resethard")
        else:
            print(f"ERROR: Cannot reach Typesense — is the server running? Run: ts start")
        print(f"  Detail: {e}")
        sys.exit(1)

    return result, query_by


def _hl_values(hl: dict) -> list:
    """Extract clean text values from a Typesense highlight entry.

    Fields listed in highlight_full_fields return 'values' (string[]) for array
    fields and 'value' (string) for scalar fields — not 'snippet'.  Fields not in
    highlight_full_fields return 'snippet'.  This helper abstracts that away.
    """
    import re as _re
    def _strip(s):
        return _re.sub(r"</?mark>", "", s).strip()
    vals = hl.get("values") or []
    if vals:
        return [_strip(v) for v in vals if v.strip()]
    fallback = hl.get("value") or hl.get("snippet") or ""
    return [_strip(fallback)] if fallback.strip() else []


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
        rel = doc["relative_path"]
        ns = doc.get("namespace", "")

        print(f"{i}. {rel}")

        # Show C# symbol context
        class_names = doc.get("class_names", [])
        method_names = doc.get("method_names", [])
        base_types = doc.get("base_types", [])
        method_sigs = doc.get("method_sigs", [])
        attributes = doc.get("attributes", [])
        usings = doc.get("usings", [])

        # Build highlight map for quick lookup
        hl_map = {hl.get("field", ""): hl for hl in hit.get("highlights", [])}

        if class_names:
            print(f"   Classes    : {', '.join(class_names[:5])}")
        if base_types:
            print(f"   Implements : {', '.join(base_types[:5])}")
        if method_sigs:
            if debug:
                print(f"   Signatures ({len(method_sigs)}):")
                for s in method_sigs:
                    s = s.encode("ascii", errors="replace").decode("ascii")
                    print(f"     {s}")
            else:
                # When method_sigs matched, show the matched sigs (not just first 3)
                matched_sigs = _hl_values(hl_map["method_sigs"]) if "method_sigs" in hl_map else []
                display = matched_sigs[:3] if matched_sigs else method_sigs[:3]
                print(f"   Signatures : {'; '.join(display)}")
        elif method_names:
            print(f"   Members    : {', '.join(method_names[:6])}")
        if attributes:
            print(f"   Attributes : {', '.join(attributes[:5])}")
        if usings:
            print(f"   Usings     : {', '.join(usings[:4])}")
        if ns:
            print(f"   NS         : {ns}")

        highlights = hit.get("highlights", [])
        if debug:
            if not highlights:
                print(f"   [debug] highlights: (empty)")
            for hl in highlights:
                field = hl.get("field", "")
                # Dump raw highlight structure so we can see what Typesense returns
                raw_keys = {k: type(v).__name__ for k, v in hl.items() if k != "field"}
                vals = _hl_values(hl)
                matched = hl.get("matched_tokens", [])
                txt = "; ".join(v.replace("\n", " ") for v in vals if v)
                txt = txt.encode("ascii", errors="replace").decode("ascii")
                tokens_flat = [t for sub in matched for t in (sub if isinstance(sub, list) else [sub])]
                print(f"   [{field}] keys={list(raw_keys)} tokens={tokens_flat} : {txt[:200]}")
        else:
            # Show a match snippet for content/semantic fields
            for hl in highlights:
                if hl.get("field") in ("content", "method_sigs", "base_types", "call_sites",
                                       "cast_sites", "type_refs", "attributes"):
                    vals = _hl_values(hl)
                    snippet = "; ".join(v.replace("\n", " ") for v in vals if v)
                    snippet = snippet.encode("ascii", errors="replace").decode("ascii")
                    if snippet:
                        print(f"   Match      : ...{snippet[:300]}...")
                    break

        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="Search query")
    ap.add_argument("--ext",    help="Filter by extension (e.g. cs, h, py)")
    ap.add_argument("--sub",    help="Filter by subsystem (e.g. myservice)")
    ap.add_argument("--limit",  type=int, default=10, help="Max results (default 10)")
    ap.add_argument("--symbols", action="store_true",
                    help="Search only C# symbol names")
    ap.add_argument("--implements", action="store_true",
                    help="[T1] Find types implementing/inheriting the query")
    ap.add_argument("--calls", action="store_true",
                    help="[T1] Find files that call the queried method")
    ap.add_argument("--sig", action="store_true",
                    help="[T1] Search method signatures (return type + param types)")
    ap.add_argument("--uses", action="store_true",
                    help="[T2] Find files that reference the queried type")
    ap.add_argument("--attrs", action="store_true",
                    help="[T2] Find files decorated with the queried attribute")
    ap.add_argument("--casts", action="store_true",
                    help="[T1] Find files with explicit casts to the queried type")
    ap.add_argument("--facets", action="store_true",
                    help="Show subsystem/extension facet counts in output")
    ap.add_argument("--debug", action="store_true",
                    help="Show matched fields, full signature list, and raw match details per result")
    ap.add_argument("--json", action="store_true",
                    help="Output raw JSON from Typesense")
    args = ap.parse_args()

    result, query_by = search(
        query=args.query,
        ext=args.ext,
        sub=args.sub,
        limit=args.limit,
        symbols_only=args.symbols,
        implements=args.implements,
        calls=args.calls,
        sig=args.sig,
        uses=args.uses,
        attrs=args.attrs,
        casts=args.casts,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        format_results(result, args.query, query_by, show_facets=args.facets, debug=args.debug)


if __name__ == "__main__":
    main()
