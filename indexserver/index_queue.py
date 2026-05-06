"""
Centralised, deduplicating index queue for all Typesense writes.

Every write to Typesense — from the initial full-index walk, the WSL file
watcher, and the Windows native watcher — flows through this single queue.
A background worker thread drains it to Typesense in batches.

Deduplication: the queue is keyed by (collection, file_id).  If the same
file is enqueued twice before the worker picks it up, the second enqueue
overwrites the action in-place (last event wins) without adding a duplicate.

mtime tracking: each upsert item carries the file's mtime (int seconds) at
enqueue time.  Delete items carry MTIME_DELETE (None) as a sentinel — mtime
is irrelevant for deletions.
"""

from __future__ import annotations

import os
import time
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from indexserver.indexer import build_document, file_id as _file_id

# Sentinel mtime value stored in queue items for delete actions.
MTIME_DELETE = None


class _Fence:
    """Sentinel inserted into the queue. When the worker reaches it (after draining
    all preceding items), it fires the callback."""
    __slots__ = ("callback",)
    def __init__(self, cb): self.callback = cb


class IndexQueue:
    """Thread-safe, deduplicating batching queue for Typesense index writes."""

    def __init__(self, batch_size: int = 50, max_file_bytes: int = 3 * 1024 * 1024):
        self._batch_size = batch_size
        self._max_file_bytes = max_file_bytes
        self._cond  = threading.Condition()
        # Ordered so the worker drains FIFO.  Duplicate keys update in-place,
        # preserving the original insertion position (FIFO order is kept for
        # the first enqueue; subsequent enqueues of the same key only update
        # the action, not the position).
        self._items: OrderedDict[tuple, tuple] = OrderedDict()
        self._client      = None
        self._thread: threading.Thread | None = None
        self._stop        = threading.Event()
        # Counters (all protected by self._cond)
        self._n_enqueued  = 0
        self._n_deduped   = 0
        self._n_upserted  = 0
        self._n_deleted   = 0
        self._n_errors    = 0
        self._n_by_reason: dict[str, int] = {}
        self._throttle_s: float = 0.0   # inter-batch pause; grows on errors, shrinks on success
        self._t_parse_total: float = 0.0
        self._t_index_total: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, client) -> None:
        """Attach a Typesense client and start the background worker thread."""
        self._client = client
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="index-queue", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the worker to stop; waits up to *timeout* seconds."""
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
        """Add a file event.

        For upsert actions, mtime is the file's modification time (seconds).
        If mtime is not provided the file is stat'd automatically.
        For delete actions, pass mtime=MTIME_DELETE (None) — it is unused.

        reason is an optional label used for stats reporting (e.g. "new",
        "modified", "created", "deleted", "event"). It does not affect behaviour.

        Returns True if this is a new entry, False if it updated an existing
        pending entry (deduplicated — the action and mtime are overwritten with
        the latest values).
        """
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

    def enqueue_bulk(
        self,
        file_pairs,              # Iterable[SourceFile]
        collection: str,
        stop_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        """Stream SourceFile objects into the queue without a long lock hold.

        Returns (new_entries, deduped_entries).
        """
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
        """Insert a completion fence.

        The callback fires after all items currently in the queue (at the time
        fence() is called) have been flushed to Typesense. Items enqueued after
        this call are unaffected — they will be processed after the fence fires.
        """
        with self._cond:
            key = ("__fence__", object())   # unique key per fence
            self._items[key] = _Fence(callback)
            self._cond.notify()

    @property
    def depth(self) -> int:
        """Number of items currently waiting in the queue."""
        with self._cond:
            return len(self._items)

    def stats(self) -> dict:
        """Snapshot of queue counters."""
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
        while not self._stop.is_set():
            batch = self._take()
            if batch:
                regular: list = []
                for item in batch:
                    if isinstance(item, _Fence):
                        if regular:
                            self._flush(regular)
                            regular = []
                        try:
                            item.callback()
                        except Exception as e:
                            print(f"[index-queue] fence callback error: {e}", flush=True)
                    else:
                        regular.append(item)
                if regular:
                    ok = self._flush(regular)
                    if ok:
                        self._throttle_s = max(self._throttle_s - 0.5, 0.0)
                    else:
                        self._throttle_s = min(self._throttle_s + 2.0, 10.0)
                    if self._throttle_s > 0:
                        time.sleep(self._throttle_s)
            else:
                with self._cond:
                    if not self._items and not self._stop.is_set():
                        self._cond.wait(timeout=1.0)

    def _take(self) -> list:
        """Pop up to batch_size items from the front of the queue."""
        with self._cond:
            batch = []
            while self._items and len(batch) < self._batch_size:
                _, item = self._items.popitem(last=False)
                batch.append(item)
            return batch

    def _flush(self, batch: list) -> bool:
        """Build documents and write to Typesense. Returns True if all writes succeeded."""
        upserts: dict[str, list] = {}
        deletes: dict[str, list] = {}
        max_bytes = self._max_file_bytes

        def _parse_one(item):
            full_path, rel, collection, action, _mtime = item
            if action == "delete":
                return ("delete", collection, _file_id(rel))
            try:
                if os.path.getsize(full_path) > max_bytes:
                    return None
                doc = build_document(full_path, rel)
                if doc:
                    return ("upsert", collection, doc)
            except OSError:
                pass
            return None

        t_parse_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(_parse_one, batch):
                if result is None:
                    continue
                kind, coll, payload = result
                if kind == "delete":
                    deletes.setdefault(coll, []).append(payload)
                else:
                    upserts.setdefault(coll, []).append(payload)
        t_parse = time.perf_counter() - t_parse_start

        n_tried   = len(batch)
        n_built   = sum(len(v) for v in upserts.values())
        n_skipped = n_tried - n_built - sum(len(v) for v in deletes.values())

        had_errors = False
        t_index = 0.0
        for coll, docs in upserts.items():
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    results = self._client.collections[coll].documents.import_(docs, {"action": "upsert"})
                except Exception as e:
                    t_index += time.perf_counter() - t0
                    had_errors = True
                    delay = max(self._throttle_s, 1.0)
                    print(f"[index-queue] upsert error ({coll}), retrying in {delay:.1f}s: {e}", flush=True)
                    time.sleep(delay)
                    continue
                t_index += time.perf_counter() - t0
                # Import call returned — count results and break regardless of print errors.
                n_ok   = sum(1 for r in results if r.get("success"))
                n_fail = len(results) - n_ok
                with self._cond:
                    self._n_upserted += n_ok
                if n_fail:
                    first_err = next((r.get("error") for r in results if not r.get("success")), None)
                    print(f"[index-queue] import {coll}: {n_ok} ok, {n_fail} failed -- first error: {first_err}", flush=True)
                print(
                    f"[index-queue] +{n_ok} -> {coll}"
                    f"  parse={t_parse:.2f}s index={t_index:.2f}s"
                    f"  ({n_built} docs)",
                    flush=True,
                )
                break

        for coll, ids in deletes.items():
            n = 0
            for doc_id in ids:
                try:
                    self._client.collections[coll].documents[doc_id].delete()
                    n += 1
                except Exception:
                    pass
            if n:
                with self._cond:
                    self._n_deleted += n
                print(f"[index-queue] -{n} from {coll}", flush=True)

        with self._cond:
            self._t_parse_total += t_parse
            self._t_index_total += t_index
        return not had_errors
