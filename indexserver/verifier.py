"""
Index verifier: scan the file system and repair the Typesense index.

Two-phase design so it shares the batch-upsert pipeline with the full indexer:

  Phase 1 — collect
      Walk the file system and the current index (via bulk-export).
      Diff their mtimes to produce a precise list of files that need updating:
        - Missing   : file on disk but not in index
        - Stale     : file in index but mtime has changed
        - Orphaned  : entry in index but file no longer exists on disk

  Phase 2 — batch-upsert  (via indexer.index_file_list)
      Feeds only the changed/missing files into the shared pipeline that the
      full indexer also uses.  Progress is reported via the on_progress callback
      so callers can track it in memory.

  Orphan deletion runs after Phase 2.

Usage:
    python verifier.py [--src PATH] [--collection NAME] [--no-delete-orphans]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.config import (
    COLLECTION, SRC_ROOT, API_KEY, PORT, HOST, to_native_path,
)
from indexserver.indexer import (
    walk_source_files, file_id,
    get_client, ensure_collection, index_file_list,
)

BATCH_SIZE = 50


def _export_index(collection: str) -> dict[str, int]:
    """Bulk-export the collection and return {doc_id: mtime_int}.

    Uses the Typesense streaming export endpoint so large collections are not
    buffered entirely in memory.
    """
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/export"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    id_mtime: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                    if "id" in doc:
                        id_mtime[doc["id"]] = int(doc.get("mtime", 0))
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception as e:
        print(f"[verifier] WARNING: index export failed: {e}", flush=True)
    return id_mtime


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


# ── ready check ───────────────────────────────────────────────────────────────

def check_ready(src_root: str | None = None,
                collection: str | None = None,
                extensions=None) -> dict:
    """Poll the filesystem and confirm the index is fully up to date.

    Performs a complete synchronous diff without modifying the index:
      1. Exports the current index ({doc_id: mtime}) from Typesense.
      2. Walks every source file on disk, comparing mtimes.
      3. Returns counts of missing/stale/orphaned entries.

    Both conditions must hold for ready=True:
      - poll_ok: the FS walk completed without errors
      - index_ok: missing == stale == orphaned == 0

    Returns::
        {
          "ready":      bool,   # poll_ok AND index_ok
          "poll_ok":    bool,   # FS walk completed successfully
          "index_ok":   bool,   # zero missing/stale/orphaned entries
          "fs_files":   int,    # files found on disk
          "indexed":    int,    # documents currently in the index
          "missing":    int,    # on disk but not in index
          "stale":      int,    # in index but mtime has changed
          "orphaned":   int,    # in index but no longer on disk
          "duration_s": float,  # seconds the poll took
          "error":      str,    # set if poll_ok is False
        }
    """
    src  = to_native_path(src_root or SRC_ROOT)
    coll = collection or COLLECTION

    t0        = time.time()
    poll_ok   = True
    error_msg = ""

    try:
        index_map = _export_index(coll)
    except Exception as e:
        return {
            "ready": False, "poll_ok": False, "index_ok": False,
            "fs_files": 0, "indexed": 0,
            "missing": 0, "stale": 0, "orphaned": 0,
            "duration_s": round(time.time() - t0, 2),
            "error": f"index export failed: {e}",
        }

    # Use a single set: start with all index IDs, discard each FS file as we
    # walk — what remains at the end are the orphaned entries.  This avoids
    # holding both index_map keys AND a separate fs_ids set in memory at once.
    remaining = set(index_map)
    fs_files  = 0
    missing   = 0
    stale     = 0

    try:
        for sf in walk_source_files(src, extensions=extensions):
            fs_files += 1
            doc_id = file_id(sf.rel)
            remaining.discard(doc_id)
            idx_mtime = index_map.get(doc_id)
            if idx_mtime is None:
                missing += 1
            elif sf.mtime != idx_mtime:
                stale += 1
    except Exception as e:
        poll_ok   = False
        error_msg = f"filesystem walk failed: {e}"

    orphaned  = len(remaining)
    index_ok  = poll_ok and missing == 0 and stale == 0 and orphaned == 0
    return {
        "ready":      poll_ok and index_ok,
        "poll_ok":    poll_ok,
        "index_ok":   index_ok,
        "fs_files":   fs_files,
        "indexed":    len(index_map),
        "missing":    missing,
        "stale":      stale,
        "orphaned":   orphaned,
        "duration_s": round(time.time() - t0, 2),
        "error":      error_msg,
    }


# ── main ───────────────────────────────────────────────────────────────────────

def run_verify(src_root: str | None = None,
               collection: str | None = None,
               queue=None,
               delete_orphans: bool = True,
               resethard: bool = False,
               stop_event=None,
               on_complete=None,
               on_progress=None,
               extensions=None) -> None:
    """Scan the file system, diff against the index, and repair any gaps.

    Args:
        src_root:       Source root to scan (default: from config).
        collection:     Typesense collection name (default: from config).
        queue:          IndexQueue to use for async writes. When provided,
                        missing/stale files are enqueued inline during the FS
                        scan so Typesense writes begin immediately — no waiting
                        for the full walk to finish. A fence fires on_complete
                        after all enqueued items are flushed.
                        When None, index_file_list() is called synchronously
                        (backward-compat for CLI and tests).
        delete_orphans: Remove index entries for files no longer on disk.
        resethard:      Drop and recreate the collection before syncing.
        stop_event:     Optional threading.Event; when set the verifier
                        stops cleanly at the next checkpoint and marks
                        progress status as 'cancelled'.
        on_complete:    Called after all work is written to Typesense.
                        With queue: fires via fence (async).
                        Without queue: called directly before returning.
    """
    src_root  = to_native_path(src_root or SRC_ROOT)
    coll_name = collection or COLLECTION

    client = get_client()
    ensure_collection(client, resethard=resethard, collection=coll_name)

    progress: dict = {
        "status":          "running",
        "phase":           "starting",
        "started_at":      time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_update":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "collection":      coll_name,
        "src_root":        src_root,
        "fs_files":        0,
        "index_docs":      0,
        "missing":         0,
        "stale":           0,
        "orphaned":        0,
        "total_to_update": 0,
        "updated":         0,
        "deleted":         0,
        "errors":          0,
    }
    if on_progress: on_progress(progress)

    print(f"[verifier] collection : {coll_name}", flush=True)
    print(f"[verifier] source root: {src_root}", flush=True)
    t0 = time.time()

    # ── Phase 1: collect ──────────────────────────────────────────────────────

    # 1a. Export current index → {doc_id: mtime}
    print("[verifier] Phase 1/2: collecting changes…", flush=True)
    print("[verifier]   exporting current index…", flush=True)
    progress["phase"] = "collecting: exporting index"
    if on_progress: on_progress(progress)

    index_map = _export_index(coll_name)
    progress["index_docs"] = len(index_map)
    print(f"[verifier]   {len(index_map):,} documents in index", flush=True)

    # 1b+1c. Walk file system and diff inline — no intermediate list.
    # Keeps memory bounded to O(index_size) rather than O(fs_files).
    print("[verifier]   scanning file system…", flush=True)
    progress["phase"] = "collecting: scanning filesystem"
    if on_progress: on_progress(progress)

    # Start with all index IDs; discard each FS file as we walk.
    # What remains at the end is the orphaned set.
    remaining: set[str] = set(index_map)
    # Async path: enqueue files inline during the scan so the queue worker
    # can start writing to Typesense immediately, without waiting for the
    # full FS walk to finish.  Sync path still collects a list for index_file_list().
    to_update: list[tuple[str, str]] | None = [] if queue is None else None
    n_enqueued = 0
    n_fs = 0
    last_scan_print = time.time()

    for sf in walk_source_files(src_root, extensions=extensions):
        if stop_event and stop_event.is_set():
            break
        n_fs += 1
        doc_id = file_id(sf.rel)
        remaining.discard(doc_id)
        idx_mtime = index_map.get(doc_id)
        if idx_mtime is None:
            progress["missing"] += 1
            needs_update = True
            reason = "new"
        elif sf.mtime != idx_mtime:
            progress["stale"] += 1
            needs_update = True
            reason = "modified"
        else:
            needs_update = False
            reason = ""

        if needs_update:
            if queue is not None:
                queue.enqueue(sf.full_path, sf.rel, coll_name, mtime=sf.mtime, reason=reason)
                n_enqueued += 1
                progress["total_to_update"] = n_enqueued
            else:
                to_update.append((sf.full_path, sf.rel))

        now = time.time()
        if now - last_scan_print >= 15:
            progress["fs_files"] = n_fs
            if queue is not None:
                progress["phase"] = f"scanning filesystem ({n_fs:,} files, {n_enqueued:,} queued)"
            else:
                progress["phase"] = f"collecting: scanning filesystem ({n_fs:,} files)"
            if on_progress: on_progress(progress)
            print(
                f"[verifier]   [{_fmt_time(now - t0)}] scanned {n_fs:,} files  "
                f"missing={progress['missing']}  stale={progress['stale']}",
                flush=True,
            )
            last_scan_print = now

    orphaned_ids = list(remaining)
    progress["fs_files"] = n_fs
    progress["orphaned"] = len(orphaned_ids)
    if queue is None:
        progress["total_to_update"] = len(to_update)

    print(f"[verifier]   {n_fs:,} files on disk", flush=True)
    print(
        f"[verifier]   missing={progress['missing']}  "
        f"stale={progress['stale']}  orphaned={len(orphaned_ids)}",
        flush=True,
    )

    if stop_event and stop_event.is_set():
        progress["status"]      = "cancelled"
        progress["phase"]       = "cancelled"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress: on_progress(progress)
        print("[verifier] Cancelled.", flush=True)
        return

    total_to_update = n_enqueued if queue is not None else len(to_update)

    if total_to_update == 0 and not orphaned_ids:
        progress["status"]  = "complete"
        progress["phase"]   = "done (index already up to date)"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress: on_progress(progress)
        print("[verifier] Index is already up to date.", flush=True)
        return

    if queue is not None:
        # Async path: all upserts already enqueued during the scan above.
        # Handle orphan deletion, then place a fence so on_complete fires
        # after everything reaches Typesense.
        if delete_orphans and orphaned_ids:
            print(f"[verifier]   removing {len(orphaned_ids)} orphaned entries…", flush=True)
            progress["phase"] = "removing orphans"
            if on_progress: on_progress(progress)
            for doc_id in orphaned_ids:
                try:
                    client.collections[coll_name].documents[doc_id].delete()
                    progress["deleted"] += 1
                except Exception:
                    pass

        progress["status"]      = "queued"
        progress["phase"]       = f"queued ({n_enqueued:,} files in index queue)"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress: on_progress(progress)
        print(
            f"[verifier] Enqueued {n_enqueued:,} files. "
            f"deleted={progress['deleted']}  "
            f"(completion fires when queue drains)",
            flush=True,
        )

        if on_complete:
            def _fence_cb(prog=progress, t=t0, n=n_enqueued, d=progress["deleted"]):
                prog["status"]      = "complete"
                prog["phase"]       = "done"
                prog["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if on_progress: on_progress(prog)
                print(
                    f"[verifier] Done in {_fmt_time(time.time() - t)}. "
                    f"enqueued={n}  deleted={d}",
                    flush=True,
                )
                on_complete()
            queue.fence(_fence_cb)

    else:
        # Sync path (backward compat for CLI and unit tests).
        last_print = time.time()
        total_to_update = len(to_update)

        def _on_progress(n_indexed: int, n_errors: int) -> None:
            progress["updated"]     = n_indexed
            progress["errors"]      = n_errors
            progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if on_progress: on_progress(progress)
            nonlocal last_print
            now = time.time()
            if now - last_print >= 15:
                pct = n_indexed * 100 // total_to_update if total_to_update else 100
                print(
                    f"[verifier]   [{_fmt_time(now - t0)}] "
                    f"{n_indexed:,}/{total_to_update:,} ({pct}%)  "
                    f"errors={n_errors}",
                    flush=True,
                )
                last_print = now

        total_indexed, total_errors = index_file_list(
            client, to_update, coll_name,
            batch_size=BATCH_SIZE,
            on_progress=_on_progress,
            stop_event=stop_event,
        )
        progress["updated"] = total_indexed
        progress["errors"]  = total_errors

        if stop_event and stop_event.is_set():
            progress["status"]      = "cancelled"
            progress["phase"]       = "cancelled"
            progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if on_progress: on_progress(progress)
            print("[verifier] Cancelled during upsert.", flush=True)
            return

        if delete_orphans and orphaned_ids:
            print(f"[verifier]   removing {len(orphaned_ids)} orphaned entries…", flush=True)
            progress["phase"] = "removing orphans"
            if on_progress: on_progress(progress)
            for doc_id in orphaned_ids:
                try:
                    client.collections[coll_name].documents[doc_id].delete()
                    progress["deleted"] += 1
                except Exception:
                    pass

        elapsed = _fmt_time(time.time() - t0)
        progress["status"]      = "complete"
        progress["phase"]       = "done"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress: on_progress(progress)
        print(
            f"[verifier] Done in {elapsed}. "
            f"updated={total_indexed}  deleted={progress['deleted']}  "
            f"errors={total_errors}",
            flush=True,
        )
        if on_complete:
            on_complete()


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src",               default=None, help="Source root directory")
    ap.add_argument("--collection",        default=None, help="Collection name")
    ap.add_argument("--no-delete-orphans", action="store_true",
                    help="Keep index entries for files that no longer exist on disk")
    ap.add_argument("--check-ready",       action="store_true",
                    help="Run a read-only readiness check and print JSON result to stdout")
    args = ap.parse_args()

    if args.check_ready:
        import json as _json
        result = check_ready(src_root=args.src, collection=args.collection)
        print(_json.dumps(result))
    else:
        run_verify(
            src_root       = args.src,
            collection     = args.collection,
            delete_orphans = not args.no_delete_orphans,
        )
