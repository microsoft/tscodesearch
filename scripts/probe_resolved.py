"""Diagnostic: for each call site in a file, report whether the qualified-call
indexer pinned a resolved type. Surfaces the gap cases — receivers that look
identifier-like but produce no qualified form — so we can spot patterns the
var-type map should learn to handle.

Usage:
    python -m scripts.probe_resolved <file.cs> [<file2.cs> ...]
    python -m scripts.probe_resolved --dir <root> [--limit N]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from query.cs import (
    _build_var_type_map, _find_all, _text, _CsIndex, _DESCRIBE_NODE_TYPES,
    _q_all_call_site_infos,
)
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _categorise_receiver(receiver: str, resolved: bool) -> str:
    if resolved:
        return "resolved"
    if not receiver:
        return "bare"  # no receiver
    if receiver[0].isupper():
        return "pascal_unresolved"  # likely-static, still gets literal form
    return "lower_unresolved"  # gap candidate


def _scan(path: str, show_lower: int = 5):
    with open(path, "rb") as f:
        src = f.read()
    tree = _PARSER.parse(src)
    vm = _build_var_type_map(tree, src)
    idx = _CsIndex(src, tree, _DESCRIBE_NODE_TYPES)
    infos = _q_all_call_site_infos(src, idx, vm)

    counts: Counter[str] = Counter()
    lower_examples: list[tuple[str, str, int]] = []  # (receiver, method, line)

    # Match each CallSiteInfo back to its tree location for line numbers.
    # _q_all_call_site_infos walks idx.of("invocation_expression") then ctors,
    # so we re-walk in the same order to align.
    inv_nodes = idx.of("invocation_expression")
    # Build aligned (info, node) pairs for the invocation-only prefix
    # (constructors come after — without receivers — and aren't interesting
    # for this diagnostic).
    for i, info in enumerate(infos):
        if i >= len(inv_nodes):
            break
        node = inv_nodes[i]
        bucket = _categorise_receiver(info.receiver, bool(info.resolved_type))
        counts[bucket] += 1
        if bucket == "lower_unresolved" and len(lower_examples) < show_lower:
            lower_examples.append(
                (info.receiver, info.name, node.start_point[0] + 1))

    total = sum(counts.values())
    print(f"\n=== {os.path.relpath(path, _REPO)} ({total} invocations) ===")
    for b in ("resolved", "bare", "pascal_unresolved", "lower_unresolved"):
        n = counts.get(b, 0)
        pct = (100 * n / total) if total else 0
        print(f"  {b:20s} {n:5d}  ({pct:5.1f}%)")
    if lower_examples:
        print("  unresolved-lowercase examples (gap candidates):")
        for r, m, ln in lower_examples:
            print(f"    L{ln}: {r}.{m}(...)")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--show-lower", type=int, default=8)
    args = ap.parse_args()

    paths: list[str] = list(args.files)
    if args.dir:
        for root, _dirs, names in os.walk(args.dir):
            for n in names:
                if n.endswith(".cs"):
                    paths.append(os.path.join(root, n))
                    if len(paths) >= args.limit:
                        break
            if len(paths) >= args.limit:
                break

    grand: Counter[str] = Counter()
    for p in paths:
        c = _scan(p, show_lower=args.show_lower)
        grand.update(c)

    print("\n=== AGGREGATE ===")
    total = sum(grand.values())
    for b in ("resolved", "bare", "pascal_unresolved", "lower_unresolved"):
        n = grand.get(b, 0)
        pct = (100 * n / total) if total else 0
        print(f"  {b:20s} {n:6d}  ({pct:5.1f}%)")


if __name__ == "__main__":
    main()
