"""
Search the Typesense code index.

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
    --sub NAME:      filter by subsystem (e.g. myservice, core, myapp)
    --limit N:       max results (default 10)

Examples:
    search.py "IStorageProvider"
    search.py "GetItemsAsync" --ext cs --sub myservice
    search.py "WriteItemsAsync" --symbols
    search.py "circuit breaker" --sub core --limit 5
    search.py "IStorageProvider" --implements     # find implementors
    search.py "GetItemsAsync" --calls             # find call sites
    search.py "ItemInfo" --uses                   # find type references
    search.py "Obsolete" --attr                   # find by attribute
"""


import os
import sys
import json
import argparse
import urllib.error
import urllib.request
import urllib.parse

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from src.query.config import HOST, PORT, API_KEY, COLLECTION
from src.ast.cs import symbol_kind_query_by


def _ts_search(collection: str, params: dict) -> dict:
    """Send a search request to Typesense over HTTP (no typesense package needed)."""
    qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{qs}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Read the response body so callers get the actual Typesense error message
        try:
            body = json.loads(e.read())
            msg = body.get("message", "")
        except Exception:
            msg = ""
        raise urllib.error.HTTPError(
            url, e.code, msg or e.reason, e.headers, None
        ) from None


def search(query, ext=None, sub=None, limit=10,
           symbols_only=False, implements=False, calls=False,
           uses=False, attrs=False, casts=False, accesses_of=False, collection=None,
           symbol_kind=None, uses_kind=""):
    from src.query.config import COLLECTION as _DEFAULT_COLLECTION
    coll_name = collection or _DEFAULT_COLLECTION

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
        else:  # "all"
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
        "highlight_full_fields": "class_names,method_names,filename,base_types,member_sigs,return_types,param_types",
        "snippet_threshold": 30,
        "num_typos": "1",
        "prefix": "false",
    }
    if filter_by:
        params["filter_by"] = filter_by

    params["facet_by"]  = "subsystem,language,extension"

    try:
        result = _ts_search(coll_name, params)
    except urllib.error.HTTPError as e:
        ts_msg = e.reason  # already the Typesense message body (from _ts_search)
        if e.code == 404:
            if "not found" in ts_msg.lower() and "collection" in ts_msg.lower():
                print(f"ERROR: Collection '{coll_name}' not found in Typesense.")
                print(f"  The index has not been built yet (or was wiped).")
                print(f"  Run: ts index --resethard")
            else:
                print(f"ERROR: Typesense returned 404: {ts_msg}")
                print(f"  collection={coll_name!r}  query_by={params.get('query_by', '?')!r}")
        elif e.code == 400:
            print(f"ERROR: Bad search request — schema mismatch or invalid field.")
            print(f"  Detail: {ts_msg}")
            print(f"  Try: ts index --resethard")
        elif e.code == 401 or e.code == 403:
            print(f"ERROR: Typesense authentication failed (HTTP {e.code}).")
            print(f"  Check api_key in config.json matches the running server.")
        elif e.code == 503:
            print(f"ERROR: Typesense is not ready yet (HTTP 503).")
            print(f"  Wait a moment and retry, or check: ts status")
        else:
            print(f"ERROR: Typesense returned HTTP {e.code}.")
            print(f"  Detail: {e}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot reach Typesense at {HOST}:{PORT} — is the server running?")
        print(f"  Run: ts start")
        print(f"  Detail: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error talking to Typesense: {e}")
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
        member_sigs = doc.get("member_sigs", [])
        attr_names = doc.get("attr_names", [])
        usings = doc.get("usings", [])

        # Build highlight map for quick lookup
        hl_map = {hl.get("field", ""): hl for hl in hit.get("highlights", [])}

        if class_names:
            print(f"   Classes    : {', '.join(class_names[:5])}")
        if base_types:
            print(f"   Implements : {', '.join(base_types[:5])}")
        if member_sigs:
            if debug:
                print(f"   Signatures ({len(member_sigs)}):")
                for s in member_sigs:
                    s = s.encode("ascii", errors="replace").decode("ascii")
                    print(f"     {s}")
            else:
                matched_sigs = _hl_values(hl_map["member_sigs"]) if "member_sigs" in hl_map else []
                display = matched_sigs[:3] if matched_sigs else member_sigs[:3]
                print(f"   Signatures : {'; '.join(display)}")
        elif method_names:
            print(f"   Members    : {', '.join(method_names[:6])}")
        if attr_names:
            print(f"   Attributes : {', '.join(attr_names[:5])}")
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
                if hl.get("field") in ("tokens", "member_sigs", "base_types", "call_sites",
                                       "cast_types", "type_refs", "attr_names"):
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
                    help="Find files that call the queried method")
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
