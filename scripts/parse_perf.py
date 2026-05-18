"""Profile parsing of a single file.

Usage:
    python -m scripts.parse_perf <file>           # cProfile, top 30 by cumtime
    python -m scripts.parse_perf <file> --top 50  # show more rows
    python -m scripts.parse_perf <file> --raw     # dump pstats to stdout
    python -m scripts.parse_perf <file> --runs 3  # average over N runs

Reports overall build_document time plus a profiler breakdown so you can see
which AST helper is dominating (e.g. _find_all, _collect_all_refs).
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from io import StringIO
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from indexserver.indexer import build_document
from query.dispatch import describe_file


def _wall(label: str, fn, *args, **kw):
    t0 = time.perf_counter()
    result = fn(*args, **kw)
    return result, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="path to the source file to profile")
    ap.add_argument("--top", type=int, default=30, help="rows to show in profiler output")
    ap.add_argument("--runs", type=int, default=1, help="repeat parse N times and average")
    ap.add_argument("--raw", action="store_true", help="dump full pstats")
    args = ap.parse_args()

    src_path = Path(args.file).resolve()
    if not src_path.is_file():
        print(f"ERROR: not a file: {src_path}", file=sys.stderr)
        return 2

    src_bytes = src_path.read_bytes()
    ext = src_path.suffix.lower()
    size = len(src_bytes)

    print(f"file: {src_path}")
    print(f"size: {size:,} bytes  ({size / 1024:.1f} KB)")
    print(f"ext:  {ext or '(none)'}")
    print(f"runs: {args.runs}")
    print()

    # Wall-clock stage timings (no profiler overhead).
    desc_total = 0.0
    bd_total   = 0.0
    for _ in range(args.runs):
        _, t = _wall("describe_file", describe_file, src_bytes, ext)
        desc_total += t
        _, t = _wall("build_document", build_document, str(src_path), src_path.name)
        bd_total += t

    print(f"describe_file:  {desc_total / args.runs * 1000:7.1f} ms (avg)")
    print(f"build_document: {bd_total   / args.runs * 1000:7.1f} ms (avg)")
    print()

    # Profiled run -- breakdown of where time goes inside describe_file.
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(args.runs):
        describe_file(src_bytes, ext)
    profiler.disable()

    buf = StringIO()
    stats = pstats.Stats(profiler, stream=buf).strip_dirs().sort_stats("cumulative")
    if args.raw:
        stats.print_stats()
    else:
        stats.print_stats(args.top)
    print(buf.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
