"""
Centralised, deduplicating index queue for all writes to the Tantivy backend.

Every write — from the initial full-index walk, the watcher, and per-event
file changes — flows through this single queue. A background worker thread
streams items into the writer (parsing in parallel) and commits when the
queue drains, when a fence is reached, or after at most COMMIT_INTERVAL_S.

Deduplication: keyed by (collection, file_id). If the same file is enqueued
twice before the worker picks it up, the second enqueue overwrites the
action in-place (last event wins) without adding a duplicate.

mtime tracking: each upsert item carries the file's mtime (int seconds) at
enqueue time. Delete items carry MTIME_DELETE (None).
"""

from __future__ import annotations

import os
import time
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from indexserver.indexer import build_document, file_id as _file_id
from indexserver.backend import Backend

# Sentinel mtime value stored in queue items for delete actions.
MTIME_DELETE = None


class _Fence:
    """Sentinel inserted into the queue. When the worker reaches it (after draining
    all preceding items), it fires the callback."""
    __slots__ = ("callback",)
    def __init__(self, cb): self.callback = cb


BackendResolver = Callable[[str], Backend]

# Commit cadence: commit when the queue empties, when a fence is hit, when the
# buffered doc count crosses COMMIT_DOC_THRESHOLD, or after COMMIT_INTERVAL_S
# seconds since the last commit — whichever comes first. The doc threshold is
# what gives the operator a visible progress bar during bulk indexing.
COMMIT_INTERVAL_S = 5 * 60
COMMIT_DOC_THRESHOLD = 5_000

# Items pulled per parallel-parse pass. Only affects parse parallelism; commit
# cadence is independent.
PARSE_CHUNK = 64

# Number of parallel parse workers (CPU-bound tree-sitter parses).
PARSE_WORKERS = 4


class IndexQueue:
    """Thread-safe, deduplicating queue. Streams writes; commits on drain."""

    def __init__(self, batch_size: int = PARSE_CHUNK, max_file_bytes: int = 3 * 1024 * 1024):
        self._batch_size = batch_size
        self._max_file_bytes = max_file_bytes
        self._cond  = threading.Condition()
        self._items: OrderedDict[tuple, tuple] = OrderedDict()
        self._resolve: BackendResolver | None = None
        self._thread: threading.Thread | None = None
        self._stop        = threading.Event()
        # Counters
        self._n_enqueued  = 0
        self._n_deduped   = 0
        self._n_upserted  = 0
        self._n_deleted   = 0
        self._n_errors    = 0
        self._n_by_reason: dict[str, int] = {}
        self._throttle_s: float = 0.0
        self._t_parse_total: float = 0.0
        self._t_index_total: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, resolver: BackendResolver) -> None:
        """Attach a backend resolver (collection_name → Backend) and start the worker."""
        self._resolve = resolver
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="index-queue", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)

    # ── public interface ──────────────────────────────────────────────────────

    def enqueue(
        self,
        full_path: str,
        rel: str,
        collection: str,
        action: str = "upsert",
        mtime: int | None = None,
        reason: str = "",
    ) -> bool:
        if action == "upsert" and mtime is None:
            try:
                mtime = int(os.stat(full_path).st_mtime)
            except OSError:
                mtime = None

        key = (collection, _file_id(rel))
        with self._cond:
            is_new = key not in self._items
            self._items[key] = (full_path, rel, collection, action, mtime)
            if is_new:
                self._n_enqueued += 1
                if reason:
                    self._n_by_reason[reason] = self._n_by_reason.get(reason, 0) + 1
                self._cond.notify()
            else:
                self._n_deduped += 1
        return is_new

    def enqueue_bulk(self, file_pairs, collection: str, stop_event: threading.Event | None = None) -> tuple[int, int]:
        n_new = n_dedup = 0
        for sf in file_pairs:
            if stop_event and stop_event.is_set():
                break
            if self.enqueue(sf.full_path, sf.rel, collection, mtime=sf.mtime):
                n_new += 1
            else:
                n_dedup += 1
        return n_new, n_dedup

    def fence(self, callback) -> None:
        """Insert a completion fence. Callback fires after all preceding items flush."""
        with self._cond:
            key = ("__fence__", object())
            self._items[key] = _Fence(callback)
            self._cond.notify()

    @property
    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def stats(self) -> dict:
        with self._cond:
            result = {
                "depth":      len(self._items),
                "enqueued":   self._n_enqueued,
                "deduped":    self._n_deduped,
                "upserted":   self._n_upserted,
                "deleted":    self._n_deleted,
                "errors":     self._n_errors,
                "throttle_s": self._throttle_s,
                "parse_s":    round(self._t_parse_total, 2),
                "index_s":    round(self._t_index_total, 2),
            }
            if self._n_by_reason:
                result["by_reason"] = dict(self._n_by_reason)
            return result

    # ── worker ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Stream-add docs to the writer; commit on drain, fence, or timer."""
        last_commit = time.monotonic()
        # Backends that have buffered (uncommitted) writes from this worker.
        dirty: set[str] = set()

        while not self._stop.is_set():
            chunk = self._take_chunk(self._batch_size)
            if chunk:
                self._process_chunk(chunk, dirty)
                age = time.monotonic() - last_commit
                buffered = self._buffered_total(dirty)
                if dirty and (
                    self._is_empty()
                    or age >= COMMIT_INTERVAL_S
                    or buffered >= COMMIT_DOC_THRESHOLD
                ):
                    self._commit_all(dirty)
                    last_commit = time.monotonic()
            else:
                # Queue empty: commit any leftover work, then wait for new arrivals.
                if dirty:
                    self._commit_all(dirty)
                    last_commit = time.monotonic()
                with self._cond:
                    if not self._items and not self._stop.is_set():
                        self._cond.wait(timeout=COMMIT_INTERVAL_S)

        # Shutdown: best-effort final commit of buffered work.
        if dirty:
            self._commit_all(dirty)

    def _take_chunk(self, n: int) -> list:
        with self._cond:
            chunk = []
            while self._items and len(chunk) < n:
                _, item = self._items.popitem(last=False)
                chunk.append(item)
            return chunk

    def _is_empty(self) -> bool:
        with self._cond:
            return not self._items

    def _buffered_total(self, dirty: set[str]) -> int:
        if self._resolve is None:
            return 0
        n = 0
        for coll in dirty:
            backend = self._resolve(coll)
            if backend is not None:
                n += getattr(backend, "buffered_count", 0)
        return n

    def _process_chunk(self, chunk: list, dirty: set[str]) -> None:
        """Parse upserts in parallel, then stream all results into the writer.

        Fences flush+commit any preceding buffered work, then fire their
        callback inline. Stop signals abort early — the dropped items will
        be re-enqueued by the verifier on the next sync.
        """
        if self._resolve is None:
            return

        # Pull fences out so they don't get parsed.
        regular = [it for it in chunk if not isinstance(it, _Fence)]
        fences  = [it for it in chunk if isinstance(it, _Fence)]
        max_bytes = self._max_file_bytes

        def _parse_one(item):
            if self._stop.is_set():
                return None
            full_path, rel, collection, action, _mtime = item
            if action == "delete":
                return ("delete", collection, _file_id(rel))
            try:
                if os.path.getsize(full_path) > max_bytes:
                    return None
            except OSError:
                return None
            try:
                doc = build_document(full_path, rel)
                if doc:
                    return ("upsert", collection, doc)
            except OSError:
                pass
            return None

        t_parse_start = time.perf_counter()
        parsed: list[tuple[str, str, object]] = []
        with ThreadPoolExecutor(max_workers=PARSE_WORKERS) as pool:
            for result in pool.map(_parse_one, regular):
                if result is not None:
                    parsed.append(result)
        t_parse = time.perf_counter() - t_parse_start

        if self._stop.is_set():
            # Bail; verifier will re-enqueue missing files on next start.
            return

        # Stream the parsed results into the writers.
        t_add_start = time.perf_counter()
        n_added = 0
        n_deleted = 0
        for kind, coll, payload in parsed:
            if self._stop.is_set():
                break
            backend = self._resolve(coll)
            if backend is None:
                continue
            try:
                if kind == "upsert":
                    backend.add(payload)
                    n_added += 1
                else:  # delete
                    backend.delete(payload)
                    n_deleted += 1
                dirty.add(coll)
            except Exception as e:
                print(f"[index-queue] add/delete error ({coll}): {type(e).__name__}: {e}", flush=True)
        t_add = time.perf_counter() - t_add_start

        with self._cond:
            self._t_parse_total += t_parse
            self._t_index_total += t_add
            # Provisional bookkeeping — these items are buffered, not yet committed.
            # They count as "upserted" once the commit succeeds; on commit failure
            # we'd undo this, but we don't have per-batch ids tracked, so we
            # accept a small over-counting on failed commits.
            self._n_upserted += n_added
            self._n_deleted  += n_deleted

        if n_added or n_deleted:
            print(
                f"[index-queue] streamed +{n_added}/-{n_deleted} "
                f"parse={t_parse:.2f}s add={t_add:.2f}s",
                flush=True,
            )

        # Fences: commit buffered work, then fire callbacks in queue order.
        for fence in fences:
            self._commit_all(dirty)
            try:
                fence.callback()
            except Exception as e:
                print(f"[index-queue] fence callback error: {e}", flush=True)

    def _commit_all(self, dirty: set[str]) -> None:
        if self._resolve is None or not dirty:
            return
        for coll in list(dirty):
            backend = self._resolve(coll)
            if backend is None or not backend.has_pending:
                dirty.discard(coll)
                continue
            t0 = time.perf_counter()
            try:
                backend.commit()
                t_commit = time.perf_counter() - t0
                print(f"[index-queue] committed {coll} in {t_commit:.2f}s", flush=True)
            except Exception as e:
                with self._cond:
                    self._n_errors += 1
                print(f"[index-queue] commit error ({coll}): {type(e).__name__}: {e}", flush=True)
            dirty.discard(coll)
