"""
Diagnostic: dump a sample of relative_path values stored in the index and
compare to what walk_source_files currently produces. We want to know if
the rel-path representation changed between runs.
"""
import os
import sys

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

import tantivy
from query.config import load_config
from indexserver.indexer import file_id, walk_source_files, ensure_backend


def main() -> None:
    cfg = load_config()
    root = next(iter(cfg.roots.values()))
    print(f"root: {root.path}")
    print(f"collection: {root.collection}")
    backend = ensure_backend(cfg, root.collection, write=False)

    idx = backend._require_index()
    idx.reload()
    searcher = idx.searcher()
    n = searcher.num_docs
    print(f"index docs: {n:,}")

    print()
    print("--- 20 sample stored docs ---")
    result = searcher.search(tantivy.Query.all_query(), limit=20)
    for _, addr in result.hits:
        doc = searcher.doc(addr).to_dict()
        rel = (doc.get("relative_path") or [""])[0]
        did = (doc.get("id") or [""])[0]
        mt  = (doc.get("mtime") or [0])[0]
        # recompute file_id from stored rel to see if matches stored id
        recomputed = file_id(rel)
        match = "OK" if recomputed == did else "MISMATCH"
        # check whether file currently exists on disk
        local_path = root.path.rstrip("/") + "/" + rel
        exists = os.path.exists(local_path)
        print(f"  id={did[:8]} rel_id={recomputed[:8]} {match}  exists={exists}  mtime={mt}  rel={rel}")

    print()
    print("--- 10 files from fs walk ---")
    walker = walk_source_files(root.path, cfg, extensions=root.extensions)
    for i, sf in enumerate(walker):
        if i >= 10:
            break
        did = file_id(sf.rel)
        print(f"  rel={sf.rel}  id={did[:8]}  mtime={sf.mtime}")


if __name__ == "__main__":
    main()
