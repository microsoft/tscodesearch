"""
Real-time CSV debug logging for the indexer pipeline.

Off by default. Enable via the ``csv_debug`` key in ``config.json`` --
either a truthy value (``true``, ``"1"``, ``"on"``) or an explicit
directory path. Truthy values write CSVs under ``<daemon-run-dir>/csv/``;
a path string overrides that location. The daemon calls ``configure()``
once at startup with the value from config; an empty / falsy value leaves
every log call as a no-op.

One CSV file per event type lives in the log directory; rows are appended
across daemon restarts so post-mortem analysis can span multiple sessions.
Every row starts with a millisecond timestamp and the daemon PID so a
sequence can be reconstructed even when several restarts share the file.

Events written today:

    session         daemon start/stop boundaries
    backend_export  one row per (doc_id, mtime, relative_path) the verifier
                    sees when it exports the index map at the start of a scan
    fs_walk         one row per file the verifier walks; ``decision`` is
                    one of ``matched``, ``missing``, ``stale``
    orphan          one row per index doc that didn't match any file on disk
                    (about to be deleted from the index)
    enqueue         one row per index queue enqueue (or dedup hit)
    parse           one row per parse the index queue worker performed
    commit          one row per Tantivy commit (success or failure)
    watcher         one row per filesystem event picked up by the watcher

The module does its own file handling: line-buffered append-mode files so
each row hits disk immediately (real-time). All writes guard against
exceptions so a logging bug can never crash the daemon.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO


# -- module state ---------------------------------------------------------------

_enabled: bool = False
_dir: Path | None = None
_files: dict[str, IO[str]] = {}
_lock = threading.Lock()
_pid: int = os.getpid()


# -- public API -----------------------------------------------------------------

def configure(default_dir: Path, setting: str = "") -> None:
    """Turn logging on if ``setting`` is non-empty / truthy.

    ``default_dir`` is used when ``setting`` is a plain truthy value
    (``1``, ``true``, ...). Any other non-empty string is treated as an
    explicit directory path that overrides ``default_dir``.
    """
    global _enabled, _dir
    raw = (setting or "").strip()
    if not raw:
        return
    if raw.lower() in ("0", "false", "no", "off"):
        return
    if raw.lower() in ("1", "true", "yes", "on"):
        _dir = default_dir
    else:
        _dir = Path(raw)
    try:
        _dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[csv_log] cannot create {_dir}: {e}", file=sys.stderr, flush=True)
        return
    _enabled = True
    print(f"[csv_log] CSV debug logging enabled -> {_dir}", flush=True)
    # Mark a session boundary so multi-restart logs can be split apart.
    session("start")


def enabled() -> bool:
    return _enabled


def shutdown(reason: str = "stop") -> None:
    """Flush+close all open csv files. Safe to call multiple times."""
    if not _enabled:
        return
    session(reason)
    with _lock:
        for f in _files.values():
            try:
                f.flush()
                f.close()
            except Exception:
                pass
        _files.clear()


# -- per-event helpers ----------------------------------------------------------

def session(action: str, detail: str = "") -> None:
    _write("session",
           ("ts", "pid", "action", "detail"),
           (action, detail))


def backend_export(collection: str, doc_id: str, mtime: int, relative_path: str) -> None:
    _write("backend_export",
           ("ts", "pid", "collection", "doc_id", "mtime", "relative_path"),
           (collection, doc_id, mtime, relative_path))


def fs_walk(collection: str, rel: str, mtime: int, doc_id: str,
            idx_mtime: int | None, decision: str) -> None:
    _write("fs_walk",
           ("ts", "pid", "collection", "rel", "mtime", "doc_id",
            "idx_mtime", "decision"),
           (collection, rel, mtime, doc_id,
            "" if idx_mtime is None else idx_mtime, decision))


def orphan(collection: str, doc_id: str) -> None:
    _write("orphan",
           ("ts", "pid", "collection", "doc_id"),
           (collection, doc_id))


def enqueue(collection: str, rel: str, action: str, mtime: int | None,
            reason: str, is_new: bool) -> None:
    _write("enqueue",
           ("ts", "pid", "collection", "rel", "action", "mtime",
            "reason", "is_new"),
           (collection, rel, action,
            "" if mtime is None else mtime, reason, "1" if is_new else "0"))


def parse(collection: str, rel: str, size: int, parse_ms: float, ok: bool,
          error: str = "") -> None:
    _write("parse",
           ("ts", "pid", "collection", "rel", "size", "parse_ms", "ok", "error"),
           (collection, rel, size, f"{parse_ms:.1f}", "1" if ok else "0", error))


def commit(collection: str, n_buffered: int, duration_ms: float,
           success: bool, error: str = "") -> None:
    _write("commit",
           ("ts", "pid", "collection", "n_buffered", "duration_ms",
            "success", "error"),
           (collection, n_buffered, f"{duration_ms:.1f}",
            "1" if success else "0", error))


def watcher(collection: str, src_path: str, action: str) -> None:
    _write("watcher",
           ("ts", "pid", "collection", "src_path", "action"),
           (collection, src_path, action))


# -- internals ------------------------------------------------------------------

def _ts() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) \
           + f".{int((now - int(now)) * 1000):03d}"


def _escape(value: Any) -> str:
    s = "" if value is None else str(value)
    # ASCII only -- replace any stray non-ASCII (paths can contain anything).
    s = s.encode("ascii", "replace").decode("ascii")
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _get_file(event: str, header: tuple[str, ...]) -> IO[str] | None:
    if _dir is None:
        return None
    f = _files.get(event)
    if f is not None:
        return f
    with _lock:
        f = _files.get(event)
        if f is not None:
            return f
        path = _dir / f"{event}.csv"
        new_file = not path.exists() or path.stat().st_size == 0
        f = open(path, "a", encoding="ascii", errors="replace",
                 buffering=1, newline="")
        if new_file:
            f.write(",".join(header) + "\n")
        _files[event] = f
        return f


def _write(event: str, header: tuple[str, ...], row: tuple) -> None:
    if not _enabled:
        return
    try:
        f = _get_file(event, header)
        if f is None:
            return
        cells = [_ts(), str(_pid)] + [_escape(v) for v in row]
        f.write(",".join(cells) + "\n")
    except Exception as e:
        # Logging must never crash the daemon. Print and move on.
        print(f"[csv_log] write error ({event}): {e}", file=sys.stderr, flush=True)
