"""
File watcher: monitors source roots for changes and enqueues them for indexing.

Runs natively in WSL. Windows paths from config (e.g. C:/myproject/src) are
automatically converted to WSL mount paths (/mnt/c/myproject/src).
Uses PollingObserver because inotify does not propagate from Windows-backed
/mnt/ filesystems in WSL.

Usage:
    python watcher.py
    python watcher.py --src /mnt/c/myrepo --collection my_collection
"""

import os
import sys
import time
import threading
import argparse

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from indexserver.config import (
    INCLUDE_EXTENSIONS, EXCLUDE_DIRS, ROOTS, collection_for_root,
)

DEBOUNCE_SECONDS  = 2.0
POLL_INTERVAL_SEC = 10   # polling interval for PollingObserver on /mnt/ paths


def _to_wsl_path(path: str) -> str:
    """Convert a Windows-style drive path to a WSL mount path.

    Examples:
        C:/myproject/src    ->  /mnt/c/myproject/src
        C:\\myproject\\src  ->  /mnt/c/myproject/src
        /mnt/c/...        ->  (unchanged)
    """
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


class CsChangeHandler(FileSystemEventHandler):
    def __init__(self, queue, src_root: str, collection: str):
        super().__init__()
        self._queue      = queue
        self.src_root    = src_root
        self._collection = collection
        self._pending    = {}
        self._lock       = threading.Lock()
        self._timer      = None

    def _schedule_flush(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _is_indexed(self, path):
        return os.path.splitext(path)[1].lower() in INCLUDE_EXTENSIONS

    def _is_excluded(self, path):
        parts = path.replace("\\", "/").split("/")
        return any(p in EXCLUDE_DIRS or p.startswith(".") for p in parts)

    def on_created(self, event):
        if not event.is_directory and self._is_indexed(event.src_path):
            if not self._is_excluded(event.src_path):
                with self._lock:
                    self._pending[event.src_path] = "upsert"
                self._schedule_flush()

    def on_modified(self, event):
        if not event.is_directory and self._is_indexed(event.src_path):
            if not self._is_excluded(event.src_path):
                with self._lock:
                    self._pending[event.src_path] = "upsert"
                self._schedule_flush()

    def on_deleted(self, event):
        if not event.is_directory and self._is_indexed(event.src_path):
            with self._lock:
                self._pending[event.src_path] = "delete"
            self._schedule_flush()

    def on_moved(self, event):
        if not event.is_directory:
            if self._is_indexed(event.src_path):
                with self._lock:
                    self._pending[event.src_path] = "delete"
            if self._is_indexed(event.dest_path) and not self._is_excluded(event.dest_path):
                with self._lock:
                    self._pending[event.dest_path] = "upsert"
            self._schedule_flush()

    def _flush(self):
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()

        n_new = n_dedup = 0
        for path, action in pending.items():
            rel = os.path.relpath(path, self.src_root).replace("\\", "/")
            if self._queue.enqueue(path, rel, self._collection, action):
                n_new += 1
            else:
                n_dedup += 1

        if n_new or n_dedup:
            dedup_note = f"  ({n_dedup} deduped)" if n_dedup else ""
            print(f"[watcher] queued {n_new} file(s){dedup_note}", flush=True)


def run_watcher(src_root=None, collection=None, stop_event=None, queue=None):
    """Watch one or all configured roots for file changes.

    Changes are enqueued into *queue* (an IndexQueue) for async processing.
    If both src_root and collection are given, watches only that root.
    Otherwise watches every root in ROOTS config.
    Windows-style paths (C:/...) are automatically converted to WSL paths.
    """
    if queue is None:
        raise ValueError("run_watcher requires a queue argument")

    if src_root is not None and collection is not None:
        wsl_path = _to_wsl_path(src_root)
        roots_map = {wsl_path: collection}
    else:
        roots_map = {
            _to_wsl_path(r): collection_for_root(name)
            for name, r in ROOTS.items()
        }

    observers = []
    for src_native, coll_name in roots_map.items():
        handler = CsChangeHandler(queue, src_native, collection=coll_name)
        obs = PollingObserver(timeout=POLL_INTERVAL_SEC)
        obs.schedule(handler, src_native, recursive=True)
        obs.start()
        observers.append(obs)
        print(f"[watcher] Watching {src_native} -> {coll_name}")

    try:
        while not (stop_event and stop_event.is_set()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    for obs in observers:
        obs.stop()
    for obs in observers:
        obs.join()
    print("[watcher] Stopped.", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Watch source files and update Typesense index")
    ap.add_argument("--src",        default=None, help="Single source root to watch")
    ap.add_argument("--collection", default=None, help="Collection for --src")
    args = ap.parse_args()
    # Standalone mode: create a minimal queue backed by a direct Typesense client
    import typesense
    from indexserver.config import TYPESENSE_CLIENT_CONFIG
    from indexserver.index_queue import IndexQueue
    q = IndexQueue()
    q.start(typesense.Client(TYPESENSE_CLIENT_CONFIG))
    run_watcher(src_root=args.src, collection=args.collection, queue=q)
    q.stop()
