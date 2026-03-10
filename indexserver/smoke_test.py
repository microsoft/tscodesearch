"""
Quick smoke test for the Typesense index server.

Checks:
  1. Server is reachable and healthy
  2. Each configured root has a collection with documents
  3. A wildcard search against each collection returns results

Exit code 0 = all checks passed, 1 = one or more failed.
No external packages required — stdlib only.
"""

import sys
import os
import json
import urllib.request
import urllib.parse

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.config import HOST, PORT, API_KEY, ROOTS, collection_for_root

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


def _get(path: str, timeout: int = 5) -> dict:
    url = f"http://{HOST}:{PORT}{path}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _search(collection: str, params: dict, timeout: int = 5) -> dict:
    qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{qs}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _check(label: str, fn) -> bool:
    try:
        ok, msg = fn()
        print(f"  [{_PASS if ok else _FAIL}] {label}: {msg}")
        return ok
    except Exception as e:
        print(f"  [{_FAIL}] {label}: {e}")
        return False


def main():
    print(f"Smoke test: http://{HOST}:{PORT}")
    print()

    results = []

    # ── 1. Server health ──────────────────────────────────────────────────────
    def _health():
        data = _get("/health")
        ok = bool(data.get("ok"))
        return ok, "healthy" if ok else f"unhealthy: {data}"

    results.append(_check("Server health", _health))

    # ── 2. Per-root collection + search ───────────────────────────────────────
    for root_name, src_path in ROOTS.items():
        coll = collection_for_root(root_name)

        def _collection(c=coll, n=root_name):
            data = _get(f"/collections/{c}")
            ndocs = data.get("num_documents", 0)
            ok = ndocs > 0
            hint = f" — run: ts index --root {n} --resethard" if not ok else ""
            return ok, f"{ndocs:,} docs in '{c}'{hint}"

        results.append(_check(f"Collection [{root_name}]", _collection))

        def _search_check(c=coll):
            data = _search(c, {"q": "*", "query_by": "filename", "per_page": "1"})
            found = data.get("found", 0)
            ok = found > 0
            return ok, f"wildcard search → {found} hits"

        results.append(_check(f"Search     [{root_name}]", _search_check))

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    passed = sum(results)
    total  = len(results)
    if passed == total:
        print(f"All {total} checks passed.")
        sys.exit(0)
    else:
        print(f"{passed}/{total} checks passed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
