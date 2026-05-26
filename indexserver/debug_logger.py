"""
Central debug logger for real-time CSV diagnostics.

Off by default. Enable via the ``csv_debug`` key in ``config.json``:
- ``true`` / ``1`` / ``on`` writes under ``<repo>/.tantivy_csv/``
- any other non-empty string is treated as an explicit output directory

Rows are append-only and line-buffered for real-time visibility. Every row is
prefixed with millisecond timestamp + daemon PID so multi-restart analysis can
split sessions reliably.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, IO


class CsvDebugHandler(logging.Handler):
    """Logging handler that appends structured rows to per-event CSV files."""

    def __init__(self, directory: Path):
        super().__init__(level=logging.DEBUG)
        self._dir = directory
        self._files: dict[str, IO[str]] = {}
        self._lock = threading.Lock()
        self._pid = os.getpid()

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "csv_event", "")
        header = getattr(record, "csv_header", ())
        row = getattr(record, "csv_row", ())
        if not event or not header:
            return
        try:
            f = self._get_file(event, header)
            if f is None:
                return
            cells = [self._timestamp(record.created), str(self._pid)] + [self._escape(v) for v in row]
            f.write(",".join(cells) + "\n")
        except Exception as e:
            _RUNTIME_LOG.warning("write error (%s): %s", event, e)

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                try:
                    f.flush()
                    f.close()
                except Exception as e:
                    _RUNTIME_LOG.warning("failed to close CSV file handle: %s", e)
            self._files.clear()
        super().close()

    def _get_file(self, event: str, header: tuple[str, ...]) -> IO[str] | None:
        f = self._files.get(event)
        if f is not None:
            return f
        with self._lock:
            f = self._files.get(event)
            if f is not None:
                return f
            path = self._dir / f"{event}.csv"
            new_file = not path.exists() or path.stat().st_size == 0
            opened = open(path, "a", encoding="ascii", errors="replace", buffering=1, newline="")
            try:
                if new_file:
                    opened.write(",".join(header) + "\n")
                self._files[event] = opened
                return opened
            except Exception:
                try:
                    opened.close()
                except Exception as close_err:
                    _RUNTIME_LOG.debug("failed closing partially initialized CSV file %s: %s", path, close_err)
                raise

    @staticmethod
    def _escape(value: Any) -> str:
        s = "" if value is None else str(value)
        s = s.encode("ascii", "replace").decode("ascii")
        if any(c in s for c in (",", '"', "\n", "\r")):
            s = '"' + s.replace('"', '""') + '"'
        return s

    @staticmethod
    def _timestamp(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) + f".{int((ts - int(ts)) * 1000):03d}"


_LOGGER_NAME = "tscodesearch.debugcsv"
_LOGGER = logging.getLogger(_LOGGER_NAME)
_LOGGER.setLevel(logging.DEBUG)
_LOGGER.propagate = False
_RUNTIME_LOG = logging.getLogger("tscodesearch.debug_logger")

_HANDLER: CsvDebugHandler | None = None
_STATE_LOCK = threading.Lock()


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def bind(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return *fn* when logging is enabled, otherwise a no-op lambda.

    Useful in hot loops where call sites want to avoid repeated
    ``if enabled()`` branches.
    """
    return fn if enabled() else _noop


def bind_lazy(fn: Callable[..., Any]) -> Callable[[Callable[[], tuple[Any, ...]]], Any]:
    """Return a lazy logger wrapper for *fn*.

    The returned callable expects a zero-arg factory that builds positional
    arguments for *fn*. The factory is invoked only when logging is enabled.
    """
    if not enabled():
        return _noop

    def _lazy(factory: Callable[[], tuple[Any, ...]]) -> Any:
        return fn(*factory())

    return _lazy


def configure(default_dir: Path, setting: str = "") -> None:
    global _HANDLER
    raw = (setting or "").strip()
    target: Path | None = None

    if raw and raw.lower() not in ("0", "false", "no", "off"):
        if raw.lower() in ("1", "true", "yes", "on"):
            target = default_dir
        else:
            target = Path(raw)

    with _STATE_LOCK:
        if _HANDLER is not None:
            try:
                _LOGGER.removeHandler(_HANDLER)
            except Exception as e:
                _RUNTIME_LOG.debug("failed to remove previous CSV debug handler: %s", e)
            try:
                _HANDLER.close()
            except Exception as e:
                _RUNTIME_LOG.debug("failed to close previous CSV debug handler: %s", e)
            _HANDLER = None

        if target is None:
            return

        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _RUNTIME_LOG.warning("cannot create %s: %s", target, e)
            return

        _HANDLER = CsvDebugHandler(target)
        _LOGGER.addHandler(_HANDLER)
        _RUNTIME_LOG.info("enabled -> %s", target)

    if enabled():
        session("start")


def enabled() -> bool:
    return _HANDLER is not None and _LOGGER.isEnabledFor(logging.DEBUG)


def shutdown(reason: str = "stop") -> None:
    global _HANDLER
    if not enabled():
        return
    session(reason)
    with _STATE_LOCK:
        if _HANDLER is not None:
            try:
                _LOGGER.removeHandler(_HANDLER)
            except Exception as e:
                _RUNTIME_LOG.debug("failed to remove CSV debug handler during shutdown: %s", e)
            try:
                _HANDLER.close()
            except Exception as e:
                _RUNTIME_LOG.debug("failed to close CSV debug handler during shutdown: %s", e)
            _HANDLER = None


def session(action: str, detail: str = "") -> None:
    _write("session", ("ts", "pid", "action", "detail"), (action, detail))


def backend_export(collection: str, doc_id: str, mtime: int, relative_path: str) -> None:
    _write(
        "backend_export",
        ("ts", "pid", "collection", "doc_id", "mtime", "relative_path"),
        (collection, doc_id, mtime, relative_path),
    )


def fs_walk(collection: str, rel: str, mtime: int, doc_id: str,
            idx_mtime: int | None, decision: str) -> None:
    _write(
        "fs_walk",
        ("ts", "pid", "collection", "rel", "mtime", "doc_id", "idx_mtime", "decision"),
        (collection, rel, mtime, doc_id, "" if idx_mtime is None else idx_mtime, decision),
    )


def orphan(collection: str, doc_id: str) -> None:
    _write("orphan", ("ts", "pid", "collection", "doc_id"), (collection, doc_id))


def enqueue(collection: str, rel: str, action: str, mtime: int | None,
            reason: str, is_new: bool) -> None:
    _write(
        "enqueue",
        ("ts", "pid", "collection", "rel", "action", "mtime", "reason", "is_new"),
        (collection, rel, action, "" if mtime is None else mtime, reason, "1" if is_new else "0"),
    )


def parse(collection: str, rel: str, size: int, parse_ms: float, ok: bool,
          error: str = "") -> None:
    _write(
        "parse",
        ("ts", "pid", "collection", "rel", "size", "parse_ms", "ok", "error"),
        (collection, rel, size, f"{parse_ms:.1f}", "1" if ok else "0", error),
    )


def commit(collection: str, n_buffered: int, duration_ms: float,
           success: bool, error: str = "") -> None:
    _write(
        "commit",
        ("ts", "pid", "collection", "n_buffered", "duration_ms", "success", "error"),
        (collection, n_buffered, f"{duration_ms:.1f}", "1" if success else "0", error),
    )


def watcher(collection: str, src_path: str, action: str) -> None:
    _write("watcher", ("ts", "pid", "collection", "src_path", "action"), (collection, src_path, action))


def _write(event: str, header: tuple[str, ...], row: tuple[Any, ...]) -> None:
    if not enabled():
        return
    _LOGGER.debug(
        "csv",
        extra={
            "csv_event": event,
            "csv_header": header,
            "csv_row": row,
        },
    )
