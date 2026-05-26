"""
Index verifier: scan the file system and repair the Tantivy index.

Two-phase design:
  Phase 1 -- collect: walk fs + read backend.export_id_mtime(); diff into
            missing/stale/orphaned sets.
  Phase 2 -- batch-upsert via indexer.index_file_list (sync) or via the
            IndexQueue (async, lets writes stream while we walk).

Orphan deletion runs after Phase 2.

Usage:
    python verifier.py [--src PATH] [--collection NAME] [--no-delete-orphans]
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from query.config import normalize_path
from indexserver.indexer import (
    walk_source_files, file_id,
    ensure_backend, index_file_list,
    export_index_map,
)

BATCH_SIZE = 50
_CSV_LOGGER = logging.getLogger("tscodesearch.debugcsv")
_LOG = logging.getLogger("tscodesearch.verifier")


def _log_csv(event: str, header: tuple[str, ...], row: tuple) -> None:
    if not _CSV_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _CSV_LOGGER.debug(
        "csv",
        extra={
            "csv_event": event,
            "csv_header": header,
            "csv_row": row,
        },
    )


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


# -- ready check ---------------------------------------------------------------

def check_ready(cfg, src_root: str | None = None,
                collection: str | None = None,
                extensions=None) -> dict:
    """Read-only diff of fs vs index. Same shape as before."""
    src  = normalize_path(src_root or cfg.src_root)
    coll = collection or cfg.collection

    t0        = time.time()
    poll_ok   = True
    error_msg = ""

    try:
        backend = ensure_backend(cfg, coll, write=False)
        index_map = export_index_map(backend, collection=coll)
    except Exception as e:
        return {
            "ready": False, "poll_ok": False, "index_ok": False,
            "fs_files": 0, "indexed": 0,
            "missing": 0, "stale": 0, "orphaned": 0,
            "duration_s": round(time.time() - t0, 2),
            "error": f"index export failed: {e}",
        }

    remaining = set(index_map)
    fs_files  = 0
    missing   = 0
    stale     = 0

    try:
        for sf in walk_source_files(src, cfg, extensions=extensions):
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


# -- main -----------------------------------------------------------------------

def run_verify(cfg, src_root: str | None = None,
               collection: str | None = None,
               queue=None,
               delete_orphans: bool = True,
               stop_event=None,
               on_complete=None,
               on_progress=None,
               extensions=None,
               backend=None) -> None:
    """Scan the file system, diff against the index, and repair any gaps.

    If `backend` is provided, uses it directly (the daemon shares one writer
    across operations). Otherwise opens its own writer.
    """
    src_root  = normalize_path(src_root or cfg.src_root)
    coll_name = collection or cfg.collection

    with contextlib.ExitStack() as _stack:
        if backend is None:
            backend = _stack.enter_context(ensure_backend(cfg, coll_name))
        _verify_with_backend(cfg, src_root, coll_name, queue, on_progress,
                             on_complete, delete_orphans, stop_event,
                             extensions, backend)


def _verify_with_backend(cfg, src_root, coll_name, queue, on_progress,
                         on_complete, delete_orphans, stop_event, extensions,
                         backend):
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
    if on_progress:
        on_progress(progress)

    _LOG.info("collection : %s", coll_name)
    _LOG.info("source root: %s", src_root)
    t0 = time.time()

    _LOG.info("Phase 1/2: collecting changes...")
    _LOG.info("  exporting current index...")
    progress["phase"] = "collecting: exporting index"
    if on_progress:
        on_progress(progress)

    index_map = export_index_map(backend, collection=coll_name)
    progress["index_docs"] = len(index_map)
    _LOG.info("  %s documents in index", f"{len(index_map):,}")

    _LOG.info("  scanning file system...")
    progress["phase"] = "collecting: scanning filesystem"
    if on_progress:
        on_progress(progress)

    remaining: set[str] = set(index_map)
    to_update: list[tuple[str, str]] = []
    n_enqueued = 0
    n_fs = 0
    last_scan_print = time.time()

    for sf in walk_source_files(src_root, cfg, extensions=extensions):
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
            decision = "missing"
        elif sf.mtime != idx_mtime:
            progress["stale"] += 1
            needs_update = True
            reason = "modified"
            decision = "stale"
        else:
            needs_update = False
            reason = ""
            decision = "matched"

        _log_csv(
            "fs_walk",
            ("ts", "pid", "collection", "rel", "mtime", "doc_id", "idx_mtime", "decision"),
            (coll_name, sf.rel, sf.mtime, doc_id, "" if idx_mtime is None else idx_mtime, decision),
        )

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
            if on_progress:
                on_progress(progress)
            _LOG.info(
                "  [%s] scanned %s files  missing=%s  stale=%s",
                _fmt_time(now - t0),
                f"{n_fs:,}",
                progress["missing"],
                progress["stale"],
            )
            last_scan_print = now

    orphaned_ids = list(remaining)
    progress["fs_files"] = n_fs
    progress["orphaned"] = len(orphaned_ids)
    for orphan_id in orphaned_ids:
        _log_csv(
            "orphan",
            ("ts", "pid", "collection", "doc_id"),
            (coll_name, orphan_id),
        )
    if queue is None:
        progress["total_to_update"] = len(to_update)

    _LOG.info("  %s files on disk", f"{n_fs:,}")
    _LOG.info(
        "  missing=%s  stale=%s  orphaned=%s",
        progress["missing"],
        progress["stale"],
        len(orphaned_ids),
    )

    if stop_event and stop_event.is_set():
        progress["status"] = "cancelled"
        progress["phase"] = "cancelled"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress:
            on_progress(progress)
        _LOG.info("Cancelled.")
        return

    total_to_update = n_enqueued if queue is not None else len(to_update)
    if total_to_update == 0 and not orphaned_ids:
        progress["status"] = "complete"
        progress["phase"] = "done (index already up to date)"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress:
            on_progress(progress)
        _LOG.info("Index is already up to date.")
        return

    if queue is not None:
        if delete_orphans and orphaned_ids:
            _LOG.info("  removing %s orphaned entries...", len(orphaned_ids))
            progress["phase"] = "removing orphans"
            if on_progress:
                on_progress(progress)
            try:
                backend.delete_many(orphaned_ids)
                progress["deleted"] = len(orphaned_ids)
            except Exception as e:
                _LOG.warning("orphan delete error: %s", e)

        progress["status"] = "queued"
        progress["phase"] = f"queued ({n_enqueued:,} files in index queue)"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress:
            on_progress(progress)
        _LOG.info(
            "Enqueued %s files. deleted=%s (completion fires when queue drains)",
            f"{n_enqueued:,}",
            progress["deleted"],
        )

        if on_complete:
            def _fence_cb(prog=progress, t=t0, n=n_enqueued, d=progress["deleted"]):
                prog["status"] = "complete"
                prog["phase"] = "done"
                prog["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                if on_progress:
                    on_progress(prog)
                _LOG.info("Done in %s. enqueued=%s  deleted=%s", _fmt_time(time.time() - t), n, d)
                on_complete()

            queue.fence(_fence_cb)
        return

    last_print = time.time()
    total_to_update = len(to_update)

    def _on_progress_index(n_indexed: int, n_errors: int) -> None:
        nonlocal last_print
        progress["updated"] = n_indexed
        progress["errors"] = n_errors
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress:
            on_progress(progress)
        now = time.time()
        if now - last_print >= 15:
            pct = n_indexed * 100 // total_to_update if total_to_update else 100
            _LOG.info(
                "  [%s] %s/%s (%s%%)  errors=%s",
                _fmt_time(now - t0),
                f"{n_indexed:,}",
                f"{total_to_update:,}",
                pct,
                n_errors,
            )
            last_print = now

    total_indexed, total_errors = index_file_list(
        backend,
        to_update,
        batch_size=BATCH_SIZE,
        on_progress=_on_progress_index,
        stop_event=stop_event,
    )
    progress["updated"] = total_indexed
    progress["errors"] = total_errors

    if stop_event and stop_event.is_set():
        progress["status"] = "cancelled"
        progress["phase"] = "cancelled"
        progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if on_progress:
            on_progress(progress)
        _LOG.info("Cancelled during upsert.")
        return

    if delete_orphans and orphaned_ids:
        _LOG.info("  removing %s orphaned entries...", len(orphaned_ids))
        progress["phase"] = "removing orphans"
        if on_progress:
            on_progress(progress)
        try:
            backend.delete_many(orphaned_ids)
            progress["deleted"] = len(orphaned_ids)
        except Exception as e:
            _LOG.warning("orphan delete error: %s", e)

    elapsed = _fmt_time(time.time() - t0)
    progress["status"] = "complete"
    progress["phase"] = "done"
    progress["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if on_progress:
        on_progress(progress)
    _LOG.info(
        "Done in %s. updated=%s  deleted=%s  errors=%s",
        elapsed,
        total_indexed,
        progress["deleted"],
        total_errors,
    )
    if on_complete:
        on_complete()


# -- entry point ----------------------------------------------------------------

if __name__ == "__main__":
    from query.config import load_config as _load_config
    _cfg = _load_config()
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
        result = check_ready(_cfg, src_root=args.src, collection=args.collection)
        print(_json.dumps(result))
    else:
        run_verify(
            _cfg,
            src_root       = args.src,
            collection     = args.collection,
            delete_orphans = not args.no_delete_orphans,
        )
