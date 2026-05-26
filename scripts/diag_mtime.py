"""
Diagnostic: compare stored mtime in the index against current disk mtime
for every file under the configured root. Categorise mismatches so we can
see whether the "stale on restart" symptom is genuine fs changes or a bug
in the indexer / verifier path.
"""

import os
import sys
from collections import Counter

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from query.config import load_config
from indexserver.indexer import file_id, walk_source_files, ensure_backend


def main() -> None:
    cfg = load_config()
    root = next(iter(cfg.roots.values()))
    print(f"root: {root.name}  path: {root.path}")
    print(f"collection: {root.collection}")
    print(f"index_dir: {root.index_dir}")
    print()

    backend = ensure_backend(cfg, root.collection, write=False)
    index_map = backend.export_id_mtime()
    print(f"index entries: {len(index_map):,}")

    matched = 0
    missing = 0
    stale = 0
    stale_diffs: Counter[int] = Counter()
    stale_examples: list = []
    missing_examples: list = []

    n_fs = 0
    for sf in walk_source_files(root.path, cfg, extensions=root.extensions):
        n_fs += 1
        doc_id = file_id(sf.rel)
        idx_mtime = index_map.get(doc_id)
        if idx_mtime is None:
            missing += 1
            if len(missing_examples) < 5:
                missing_examples.append((sf.rel, sf.mtime))
        elif sf.mtime != idx_mtime:
            stale += 1
            diff = sf.mtime - idx_mtime
            stale_diffs[diff] += 1
            if len(stale_examples) < 10:
                stale_examples.append((sf.rel, sf.mtime, idx_mtime, diff))
        else:
            matched += 1
        if n_fs % 10000 == 0:
            print(f"  scanned {n_fs:,} ...", flush=True)

    print()
    print(f"fs files:   {n_fs:,}")
    print(f"matched:    {matched:,}")
    print(f"missing:    {missing:,}")
    print(f"stale:      {stale:,}")
    print()
    print("Top 20 mtime diff buckets (disk_mtime - idx_mtime):")
    for diff, count in stale_diffs.most_common(20):
        print(f"  diff={diff:+d}s  count={count:,}")
    print()
    print("Sample stale files (rel, disk_mtime, idx_mtime, diff):")
    for rel, dm, im, d in stale_examples:
        print(f"  {rel}  disk={dm} idx={im} diff={d}")
    print()
    print("Sample missing files (rel, disk_mtime):")
    for rel, dm in missing_examples:
        print(f"  {rel}  disk={dm}")


if __name__ == "__main__":
    main()
