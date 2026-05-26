"""
Scan the CSV debug logs produced by ``indexserver.csv_log`` and surface
what the daemon decided. Built for the "stop / restart doesn't dedup"
class of bug: per-session breakdowns of how many files matched, how many
were re-indexed, what commits failed, and which files keep bouncing
between matched and stale across restarts.

Usage:
    python -m scripts.scan_csv                       # default summary
    python -m scripts.scan_csv --mode sessions       # list sessions only
    python -m scripts.scan_csv --mode stales         # per-stale-file detail
    python -m scripts.scan_csv --mode missing        # per-missing-file detail
    python -m scripts.scan_csv --mode orphans        # orphan id list
    python -m scripts.scan_csv --mode errors         # commit/parse errors
    python -m scripts.scan_csv --mode index-diff     # what changed between
                                                     # the last two index exports
    python -m scripts.scan_csv --mode flapping       # files that flipped state
                                                     # across sessions
    python -m scripts.scan_csv --session N           # filter to one session
                                                     # (1 = oldest)

Defaults to ``%LOCALAPPDATA%/tscodesearch/csv`` on Windows. Override with
``--csv-dir`` or env ``TSCODESEARCH_CSV_DIR``.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# -- paths ---------------------------------------------------------------------

def _default_csv_dir() -> Path:
    explicit = os.environ.get("TSCODESEARCH_CSV_DIR", "").strip()
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = str(Path.home() / ".local")
    return Path(base) / "tscodesearch" / "csv"


# -- session model -------------------------------------------------------------

@dataclass
class Session:
    pid: str
    start_ts: str = ""
    stop_ts: str = ""

    # backend_export: count + mtime histogram + (id -> mtime, id -> rel)
    export_count: int = 0
    export_id_to_mtime: dict[str, int] = field(default_factory=dict)
    export_id_to_rel:   dict[str, str] = field(default_factory=dict)

    # fs_walk: rows per decision; per-stale and per-missing details for drill-down
    walk_decision: Counter = field(default_factory=Counter)
    stale_rows:   list[tuple[str, int, int]] = field(default_factory=list)  # (rel, disk_mtime, idx_mtime)
    missing_rows: list[tuple[str, int]]      = field(default_factory=list)  # (rel, disk_mtime)
    matched_rels: set[str] = field(default_factory=set)
    walk_total: int = 0

    # orphan id list (deleted from index)
    orphans: list[str] = field(default_factory=list)

    # commit / parse / enqueue counters
    commits_ok:   int = 0
    commits_fail: int = 0
    commit_errors: list[tuple[str, str, str]] = field(default_factory=list)  # (ts, coll, err)
    parse_total:  int = 0
    parse_fail:   int = 0
    parse_errors: list[tuple[str, str, str]] = field(default_factory=list)
    enqueued:    int = 0
    deduped:     int = 0
    enqueue_by_reason: Counter = field(default_factory=Counter)

    # watcher
    watcher_events: Counter = field(default_factory=Counter)


# -- loader --------------------------------------------------------------------

def _iter_csv(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with open(path, newline="", encoding="ascii", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def load(csv_dir: Path) -> list[Session]:
    """Build per-session aggregates from every CSV in *csv_dir*."""
    # session.csv defines the order. Without it, we still gather by PID.
    sessions: dict[str, Session] = {}
    order: list[str] = []  # PIDs in session-start order

    def _get(pid: str) -> Session:
        s = sessions.get(pid)
        if s is None:
            s = Session(pid=pid)
            sessions[pid] = s
        return s

    for row in _iter_csv(csv_dir / "session.csv"):
        pid = row["pid"]
        s = _get(pid)
        action = row["action"]
        if action == "start":
            s.start_ts = row["ts"]
            if pid not in order:
                order.append(pid)
        elif action in ("stop", "test-done"):
            s.stop_ts = row["ts"]

    for row in _iter_csv(csv_dir / "backend_export.csv"):
        s = _get(row["pid"])
        if s.pid not in order:
            order.append(s.pid)
        if not s.start_ts:
            s.start_ts = row["ts"]
        s.export_count += 1
        did = row["doc_id"]
        try:
            s.export_id_to_mtime[did] = int(row["mtime"])
        except (TypeError, ValueError):
            s.export_id_to_mtime[did] = 0
        s.export_id_to_rel[did] = row.get("relative_path", "")

    for row in _iter_csv(csv_dir / "fs_walk.csv"):
        s = _get(row["pid"])
        if s.pid not in order:
            order.append(s.pid)
        decision = row["decision"]
        s.walk_decision[decision] += 1
        s.walk_total += 1
        rel = row["rel"]
        try:
            disk_mtime = int(row["mtime"])
        except (TypeError, ValueError):
            disk_mtime = 0
        if decision == "matched":
            s.matched_rels.add(rel)
        elif decision == "stale":
            try:
                idx_mtime = int(row["idx_mtime"])
            except (TypeError, ValueError):
                idx_mtime = 0
            s.stale_rows.append((rel, disk_mtime, idx_mtime))
        elif decision == "missing":
            s.missing_rows.append((rel, disk_mtime))

    for row in _iter_csv(csv_dir / "orphan.csv"):
        s = _get(row["pid"])
        s.orphans.append(row["doc_id"])

    for row in _iter_csv(csv_dir / "commit.csv"):
        s = _get(row["pid"])
        ok = row.get("success", "0") == "1"
        if ok:
            s.commits_ok += 1
        else:
            s.commits_fail += 1
            s.commit_errors.append((row["ts"], row["collection"], row.get("error", "")))

    for row in _iter_csv(csv_dir / "parse.csv"):
        s = _get(row["pid"])
        s.parse_total += 1
        if row.get("ok", "0") != "1":
            s.parse_fail += 1
            s.parse_errors.append((row["ts"], row["rel"], row.get("error", "")))

    for row in _iter_csv(csv_dir / "enqueue.csv"):
        s = _get(row["pid"])
        if row.get("is_new", "0") == "1":
            s.enqueued += 1
            reason = row.get("reason", "")
            if reason:
                s.enqueue_by_reason[reason] += 1
        else:
            s.deduped += 1

    for row in _iter_csv(csv_dir / "watcher.csv"):
        s = _get(row["pid"])
        s.watcher_events[row["action"]] += 1

    return [sessions[pid] for pid in order]


# -- presenters ---------------------------------------------------------------

def _bucket_diff(seconds: int) -> str:
    """Human-readable mtime delta bucket."""
    a = abs(seconds)
    if a < 60:        return f"{seconds:+d}s"
    if a < 3600:      return f"{seconds // 60:+d}m"
    if a < 86400:     return f"{seconds // 3600:+d}h"
    return f"{seconds // 86400:+d}d"


def cmd_sessions(sessions: list[Session]) -> None:
    print(f"{'#':>3}  {'pid':>6}  {'started':<23}  {'stopped':<23}  {'exported':>9}  {'walked':>8}")
    for i, s in enumerate(sessions, 1):
        print(f"{i:>3}  {s.pid:>6}  {s.start_ts or '-':<23}  {s.stop_ts or '-':<23}  "
              f"{s.export_count:>9,}  {s.walk_total:>8,}")


def cmd_summary(sessions: list[Session]) -> None:
    cmd_sessions(sessions)
    print()
    print(f"{'#':>3}  {'matched':>9}  {'stale':>7}  {'missing':>8}  {'orphans':>8}  "
          f"{'enq_new':>8}  {'enq_dup':>8}  {'commit_ok':>10}  {'commit_fail':>12}  notes")
    for i, s in enumerate(sessions, 1):
        notes = []
        if s.export_count == 0 and s.walk_total > 0:
            notes.append("INDEX_EMPTY_ON_OPEN")
        if s.walk_total and s.walk_decision.get("missing", 0) == s.walk_total:
            notes.append("ALL_MISSING")
        if s.commits_fail:
            notes.append(f"COMMIT_FAIL={s.commits_fail}")
        if s.orphans and s.export_count and len(s.orphans) > s.export_count * 0.5:
            notes.append(f"ORPHAN_HEAVY ({len(s.orphans)}/{s.export_count})")
        if s.parse_fail:
            notes.append(f"PARSE_FAIL={s.parse_fail}")
        print(
            f"{i:>3}  "
            f"{s.walk_decision.get('matched',0):>9,}  "
            f"{s.walk_decision.get('stale',0):>7,}  "
            f"{s.walk_decision.get('missing',0):>8,}  "
            f"{len(s.orphans):>8,}  "
            f"{s.enqueued:>8,}  "
            f"{s.deduped:>8,}  "
            f"{s.commits_ok:>10,}  "
            f"{s.commits_fail:>12,}  "
            f"{'  '.join(notes)}"
        )

    # Cross-session signal: did the index size collapse between two restarts?
    print()
    print("Cross-session checks:")
    if len(sessions) < 2:
        print("  (need at least two sessions)")
        return
    prev = sessions[0]
    for cur in sessions[1:]:
        delta = cur.export_count - prev.export_count
        flag = ""
        if cur.export_count == 0 and prev.export_count > 0:
            flag = "  <-- INDEX WIPED"
        elif prev.export_count and abs(delta) > prev.export_count * 0.25:
            flag = f"  <-- LARGE DELTA ({delta:+,})"
        print(f"  pid {prev.pid} -> pid {cur.pid}: exported {prev.export_count:,} -> {cur.export_count:,} ({delta:+,}){flag}")
        prev = cur


def cmd_stales(sessions: list[Session], top: int = 30, samples: int = 20) -> None:
    for i, s in enumerate(sessions, 1):
        if not s.stale_rows:
            continue
        print(f"=== session #{i} pid={s.pid}  stales={len(s.stale_rows):,} ===")
        buckets: Counter = Counter()
        for _, dm, im in s.stale_rows:
            buckets[_bucket_diff(dm - im)] += 1
        print("  top mtime-delta buckets:")
        for b, n in buckets.most_common(top):
            print(f"    {b:>10}  {n:,}")
        print("  sample stales:")
        for rel, dm, im in s.stale_rows[:samples]:
            print(f"    {_bucket_diff(dm-im):>8}  disk={dm}  idx={im}  {rel}")
        print()


def cmd_missing(sessions: list[Session], samples: int = 30) -> None:
    for i, s in enumerate(sessions, 1):
        if not s.missing_rows:
            continue
        print(f"=== session #{i} pid={s.pid}  missing={len(s.missing_rows):,} ===")
        for rel, dm in s.missing_rows[:samples]:
            print(f"    disk={dm}  {rel}")
        if len(s.missing_rows) > samples:
            print(f"    ... and {len(s.missing_rows) - samples:,} more")
        print()


def cmd_orphans(sessions: list[Session], samples: int = 30) -> None:
    for i, s in enumerate(sessions, 1):
        if not s.orphans:
            continue
        print(f"=== session #{i} pid={s.pid}  orphans={len(s.orphans):,} ===")
        # cross-reference against the export to print a relative_path if we have one
        rel_lookup = s.export_id_to_rel
        for did in s.orphans[:samples]:
            rel = rel_lookup.get(did, "(unknown)")
            print(f"    {did}  {rel}")
        if len(s.orphans) > samples:
            print(f"    ... and {len(s.orphans) - samples:,} more")
        print()


def cmd_errors(sessions: list[Session]) -> None:
    any_errors = False
    for i, s in enumerate(sessions, 1):
        if not (s.commit_errors or s.parse_errors):
            continue
        any_errors = True
        print(f"=== session #{i} pid={s.pid} ===")
        if s.commit_errors:
            print(f"  commit failures: {len(s.commit_errors)}")
            for ts, coll, err in s.commit_errors[:20]:
                print(f"    {ts}  {coll}  {err}")
            if len(s.commit_errors) > 20:
                print(f"    ... and {len(s.commit_errors) - 20} more")
        if s.parse_errors:
            print(f"  parse failures: {len(s.parse_errors)}")
            for ts, rel, err in s.parse_errors[:20]:
                print(f"    {ts}  {rel}  {err}")
            if len(s.parse_errors) > 20:
                print(f"    ... and {len(s.parse_errors) - 20} more")
        print()
    if not any_errors:
        print("No commit or parse errors recorded.")


def cmd_index_diff(sessions: list[Session]) -> None:
    """Compare the index-export set between the most recent two sessions."""
    if len(sessions) < 2:
        print("Need at least two sessions to diff.")
        return
    prev, cur = sessions[-2], sessions[-1]
    prev_ids = set(prev.export_id_to_mtime)
    cur_ids  = set(cur.export_id_to_mtime)

    disappeared = prev_ids - cur_ids
    appeared    = cur_ids - prev_ids
    common      = prev_ids & cur_ids
    mtime_changed = [
        did for did in common
        if prev.export_id_to_mtime[did] != cur.export_id_to_mtime[did]
    ]

    print(f"=== diff: session pid={prev.pid} -> pid={cur.pid} ===")
    print(f"  prev exported:    {len(prev_ids):,}")
    print(f"  cur  exported:    {len(cur_ids):,}")
    print(f"  disappeared:      {len(disappeared):,}")
    print(f"  appeared (new):   {len(appeared):,}")
    print(f"  mtime changed:    {len(mtime_changed):,}")
    print()
    if disappeared:
        print("  sample disappeared (in prev index, missing from cur):")
        for did in list(disappeared)[:15]:
            rel = prev.export_id_to_rel.get(did, "")
            mt  = prev.export_id_to_mtime.get(did, 0)
            print(f"    {did}  prev_mtime={mt}  {rel}")
    if appeared:
        print("  sample appeared (in cur index, not in prev):")
        for did in list(appeared)[:15]:
            rel = cur.export_id_to_rel.get(did, "")
            mt  = cur.export_id_to_mtime.get(did, 0)
            print(f"    {did}  cur_mtime={mt}  {rel}")


def cmd_flapping(sessions: list[Session], top: int = 30) -> None:
    """Files that switched decision (matched <-> stale/missing) across sessions."""
    if len(sessions) < 2:
        print("Need at least two sessions to detect flapping.")
        return
    per_rel: dict[str, list[str]] = defaultdict(list)
    session_rels: list[set[str]] = []
    for s in sessions:
        rels: set[str] = set()
        rels.update(s.matched_rels)
        for rel, _, _ in s.stale_rows:
            per_rel[rel].append("stale")
            rels.add(rel)
        for rel, _ in s.missing_rows:
            per_rel[rel].append("missing")
            rels.add(rel)
        # matched files contribute "matched" once
        for rel in s.matched_rels:
            per_rel[rel].append("matched")
        session_rels.append(rels)
    flap: Counter = Counter()
    for rel, states in per_rel.items():
        unique = set(states)
        if len(unique) > 1:
            flap[rel] += sum(1 for s in states if s != "matched")
    print(f"Files seen in >1 distinct decision state (top {top}):")
    for rel, score in flap.most_common(top):
        states = per_rel[rel]
        print(f"  score={score:>3}  {rel}  states={','.join(states)}")


# -- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-dir", default=None, help="CSV log dir (default: %LOCALAPPDATA%/tscodesearch/csv)")
    ap.add_argument("--mode", default="summary",
                    choices=["summary", "sessions", "stales", "missing",
                             "orphans", "errors", "index-diff", "flapping"])
    ap.add_argument("--session", type=int, default=0,
                    help="Filter output to session N (1 = oldest). 0 = all.")
    ap.add_argument("--samples", type=int, default=20)
    args = ap.parse_args(argv)

    csv_dir = Path(args.csv_dir) if args.csv_dir else _default_csv_dir()
    if not csv_dir.exists():
        print(f"CSV directory not found: {csv_dir}", file=sys.stderr)
        return 1
    print(f"CSV dir: {csv_dir}")
    print()

    sessions = load(csv_dir)
    if not sessions:
        print("No sessions found.")
        return 0

    if args.session:
        if not (1 <= args.session <= len(sessions)):
            print(f"--session must be 1..{len(sessions)}", file=sys.stderr)
            return 1
        sessions = [sessions[args.session - 1]]

    mode = args.mode
    if   mode == "sessions":     cmd_sessions(sessions)
    elif mode == "summary":      cmd_summary(sessions)
    elif mode == "stales":       cmd_stales(sessions, samples=args.samples)
    elif mode == "missing":      cmd_missing(sessions, samples=args.samples)
    elif mode == "orphans":      cmd_orphans(sessions, samples=args.samples)
    elif mode == "errors":       cmd_errors(sessions)
    elif mode == "index-diff":   cmd_index_diff(sessions)
    elif mode == "flapping":     cmd_flapping(sessions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
