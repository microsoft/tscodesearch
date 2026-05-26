"""
tsquery_server -- cross-platform management server daemon.

Runs under .client-venv (Windows) or any compatible Python on Linux/macOS.
Owns one Tantivy index per configured root and exposes an HTTP API on
_cfg.port.

Entry points
------------
  start_daemon() -> bool
      Try to bind PORT.  Returns True if this process now owns the port,
      False if another instance is already running there.

  run_until_shutdown()
      Block until a shutdown signal is received (SIGTERM, SIGINT, or
      POST /management/shutdown).

  stop_daemon()
      Signal the running daemon to stop.

HTTP endpoints:
  GET  /health               -> {"ok": true}  (no auth)
  GET  /status               -> watcher / queue / collections
  POST /file-events          -> accept file-change notifications
  POST /query-codebase       -> backend search + AST post-process
  POST /management/shutdown  -> graceful daemon shutdown (used by ts stop)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

_REPO = Path(__file__).parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from indexserver.backend import Backend
from query.config import index_root, load_config as _load_config, normalize_path
from indexserver.index_queue import IndexQueue
from indexserver.indexer import ensure_backend
from indexserver.runtime_logger import configure as _configure_runtime_logger
from indexserver.search_modes import build_filter_by, resolve_query_params
from indexserver.verifier import run_verify
from indexserver.watcher import run_watcher
from indexserver import debug_logger
from indexserver import search as _search_mod

_cfg = _load_config()
ALL_ROOTS = _cfg.roots

# -- runtime paths --------------------------------------------------------------
_HOME = Path.home()
_DEFAULT_RUN_DIR = (
    Path(os.environ.get("LOCALAPPDATA", _HOME / "AppData" / "Local")) / "tscodesearch"
    if sys.platform == "win32"
    else _HOME / ".local" / "tscodesearch"
)
_RUN_DIR        = Path(os.environ.get("TSCODESEARCH_DATA", _DEFAULT_RUN_DIR))
_DAEMON_PID     = _RUN_DIR / "daemon.pid"
_DAEMON_LOCK    = _RUN_DIR / "daemon.lock"
_CSV_DEBUG_DIR  = index_root().with_name(f"{index_root().name}_csv")

_LOG = logging.getLogger("tscodesearch.daemon")

# File handle for the OS-level exclusive lock.  Intentionally never closed --
# the OS releases the lock when the process truly exits (clean or crash), which
# prevents a second daemon from starting before the first is fully gone.
_lock_fh: object = None

_QUERY_CODEBASE_MAX_LIMIT = 250

# -- thread state ---------------------------------------------------------------
_watcher_thread: threading.Thread | None = None
_watcher_lock    = threading.Lock()

# Backends keyed by collection name. The daemon owns the writer for each.
_backends: dict[str, Backend] = {}

_index_queue = IndexQueue(max_file_bytes=_cfg.max_file_bytes)

_server: HTTPServer | None = None
_shutdown_event = threading.Event()

_verify_status_lock = threading.Lock()
_verify_status: dict = {
    "state": "idle",
    "active_root": "",
    "started_at": "",
    "last_update": "",
    "roots": {},
}


def _lock_held() -> bool:
    """True when this process currently holds the daemon file lock."""
    return _lock_fh is not None


# -- Tree-sitter query helper ---------------------------------------------------

_query_module = None

def _get_query_module():
    global _query_module
    if _query_module is None:
        import query.dispatch as _q
        _query_module = _q
    return _query_module


def _run_query(mode: str, pattern: str, files: list[Path],
               include_body: bool = False, symbol_kind: str = "",
               uses_kind: str = "", visibility: str = "",
               head_lines: int | None = None,
               enclosing_method: str = "",
               enclosing_class: str = "") -> list:
    """Per-file AST pass for the index-pre-filtered candidate set.

    Files whose language doesn't support ``mode`` are silently skipped
    rather than crashing the whole query -- a codebase-wide query like
    ``body SaveChanges`` can match files in many languages, and only the
    ones whose extractor knows ``body`` should contribute results. The
    ValueError that ``query_file`` raises for unsupported modes is
    treated as a no-op for that file.
    """
    _q = _get_query_module()
    results = []
    for path in files:
        native = path.resolve()
        ext = native.suffix.lower()
        try:
            src_bytes = native.read_bytes()
        except OSError as e:
            _LOG.warning("ERROR reading %s: %s", native, e)
            continue
        try:
            matches = _q.query_file(src_bytes, ext, mode, pattern,
                                    include_body=include_body,
                                    symbol_kind=symbol_kind,
                                    uses_kind=uses_kind,
                                    visibility=visibility,
                                    head_lines=head_lines,
                                    enclosing_method=enclosing_method or None,
                                    enclosing_class=enclosing_class or None)
        except ValueError:
            # Mode unsupported for this file's language - skip silently.
            continue
        if matches:
            results.append({"file": str(native), "matches": matches})
    return results


# -- Mode mapping ---------------------------------------------------------------

_EXT_TO_TS_AND_AST: dict[str, tuple[str, str]] = {
    "declarations": ("symbols",     "declarations"),
    "body":         ("symbols",     "body"),
    "calls":        ("calls",       "calls"),
    # ``caller_of`` shares the index pre-filter with ``calls`` -- same set
    # of candidate files contain the method invocations -- but the AST
    # post-pass groups results by the enclosing method.
    "caller_of":    ("calls",       "caller_of"),
    # ``callee_of`` is "what does THIS method call". Pre-filter on the
    # method's declaration field (``method_names``) -- the file we want is
    # the one that *declares* the method, not the ones that call it.
    "callee_of":    ("symbols",     "callee_of"),
    "implements":   ("implements",  "implements"),
    "uses":         ("uses",        "uses"),
    "casts":        ("casts",       "casts"),
    "attrs":        ("attrs",       "attrs"),
    "accesses_of":  ("accesses_of", "accesses_of"),
    "accesses_on":  ("uses",        "accesses_on"),
    "all_refs":     ("all_refs",    "all_refs"),
    "var_type":     ("all_refs",    "var_type"),
}


# -- Per-component status -------------------------------------------------------

def _watcher_status() -> dict:
    running     = bool(_watcher_thread and _watcher_thread.is_alive())
    queue_depth = _index_queue.depth
    if not running:
        state = "stopped"
    elif queue_depth > 0:
        state = "processing"
    else:
        state = "watching"
    return {
        "running":     running,
        "paused":      False,
        "state":       state,
        "queue_depth": queue_depth,
    }


def _collections_status() -> dict:
    collections: dict = {}
    for root in ALL_ROOTS.values():
        backend = _backends.get(root.collection)
        ndocs: int | None = None
        buffered = 0
        if backend is not None:
            try:
                ndocs = backend.num_documents()
            except Exception:
                pass
            buffered = getattr(backend, "buffered_count", 0)
        col_live_exists = ndocs is not None
        collections[root.name] = {
            "collection":        root.collection,
            "num_documents":     ndocs,
            "buffered":          buffered,
            "collection_exists": col_live_exists,
            "schema_ok":         col_live_exists,
            "schema_warnings":   [],
        }
    return collections


def _verify_status_begin() -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _verify_status_lock:
        _verify_status.update({
            "state": "running",
            "active_root": "",
            "started_at": now,
            "last_update": now,
            "roots": {},
        })


def _verify_status_progress(root_name: str, progress: dict) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    row = {
        "status": progress.get("status", "running"),
        "phase": progress.get("phase", ""),
        "fs_files": progress.get("fs_files", 0),
        "missing": progress.get("missing", 0),
        "stale": progress.get("stale", 0),
        "orphaned": progress.get("orphaned", 0),
        "total_to_update": progress.get("total_to_update", 0),
        "updated": progress.get("updated", 0),
        "errors": progress.get("errors", 0),
        "last_update": progress.get("last_update", now),
    }
    with _verify_status_lock:
        _verify_status["state"] = "running"
        _verify_status["active_root"] = root_name
        _verify_status["last_update"] = now
        _verify_status["roots"][root_name] = row


def _verify_status_root_error(root_name: str, error: str) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _verify_status_lock:
        _verify_status["roots"][root_name] = {
            "status": "error",
            "phase": "initial scan failed",
            "error": str(error),
            "last_update": now,
        }
        _verify_status["last_update"] = now


def _verify_status_finish(cancelled: bool = False) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _verify_status_lock:
        _verify_status["state"] = "cancelled" if cancelled else "complete"
        _verify_status["active_root"] = ""
        _verify_status["last_update"] = now


def _verify_status_snapshot() -> dict:
    with _verify_status_lock:
        roots = {name: dict(info) for name, info in _verify_status.get("roots", {}).items()}
        return {
            "state": _verify_status.get("state", "idle"),
            "active_root": _verify_status.get("active_root", ""),
            "started_at": _verify_status.get("started_at", ""),
            "last_update": _verify_status.get("last_update", ""),
            "roots": roots,
        }


# -- Watcher --------------------------------------------------------------------

def _start_watcher() -> None:
    global _watcher_thread
    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return
        _watcher_thread = threading.Thread(
            target=run_watcher,
            args=(_cfg,),
            kwargs={"stop_event": _shutdown_event, "queue": _index_queue},
            name="watcher",
            daemon=True,
        )
        _watcher_thread.start()
        _LOG.info("Watcher thread started.")


# -- File-event handler ---------------------------------------------------------

def _enqueue_file_events(events: list) -> dict:
    root_map = [
        (r.path.rstrip("/"), r.collection, r.extensions)
        for r in ALL_ROOTS.values()
    ]

    n_new = n_dedup = 0
    for ev in events:
        raw_path = normalize_path(ev.get("path", ""))
        action   = ev.get("action", "upsert")
        ext      = os.path.splitext(raw_path)[1].lower()

        coll = native_root = root_exts = None
        for nr, c, exts in root_map:
            prefix = nr.lower() + "/"
            test   = raw_path.lower()
            if test.startswith(prefix):
                native_root, coll, root_exts = nr, c, exts
                break
        if coll is None or native_root is None or root_exts is None:
            continue

        if ext not in root_exts:
            continue

        rel   = raw_path[len(native_root) + 1:]
        parts = rel.split("/")
        if any(p in _cfg.exclude_dirs or p.startswith(".") for p in parts[:-1]):
            continue

        if _index_queue.enqueue(raw_path, rel, coll, action, reason="event"):
            n_new += 1
        else:
            n_dedup += 1

    return {"queued": n_new, "deduped": n_dedup}


# -- Daemon initialization ------------------------------------------------------

def _init_backends() -> None:
    """Open one Tantivy backend per configured root and log its doc count."""
    for root in ALL_ROOTS.values():
        try:
            backend = ensure_backend(_cfg, root.collection)
            _backends[root.collection] = backend
            try:
                ndocs = backend.num_documents()
            except Exception:
                ndocs = -1
            _LOG.info(
                "Opened backend %s (%s docs on disk) at %s",
                root.collection,
                f"{ndocs:,}",
                root.index_dir,
            )
        except Exception as e:
            _LOG.warning("Could not open backend %s: %s", root.collection, e)


def _initialize_async(stop_event: threading.Event) -> None:
    """Open backends, start the queue, run initial scan, start the watcher."""
    _init_backends()
    _index_queue.start(lambda c: _backends.get(c))
    _start_watcher()
    _verify_status_begin()

    for root in ALL_ROOTS.values():
        if _shutdown_event.is_set():
            break
        try:
            def _on_progress(p: dict, root_name: str = root.name) -> None:
                _verify_status_progress(root_name, p)

            run_verify(
                _cfg,
                src_root=root.path,
                collection=root.collection,
                queue=_index_queue,
                delete_orphans=True,
                stop_event=_shutdown_event,
                extensions=root.extensions,
                on_complete=lambda: None,
                on_progress=_on_progress,
                backend=_backends.get(root.collection),
            )
        except Exception as e:
            _verify_status_root_error(root.name, str(e))
            _LOG.error("Initial scan error (%s): %s", root.collection, e)

    _verify_status_finish(cancelled=_shutdown_event.is_set())

    _LOG.info("Initialization complete.")


# -- HTTP handler ---------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Silence the default per-request stderr access log.
        pass

    def _auth(self) -> bool:
        return self.headers.get("X-API-KEY") == _cfg.api_key

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                pass
        return {}

    def _handle(self) -> None:
        path   = self.path.split("?")[0].rstrip("/")
        method = self.command

        # GET /health (no auth)
        if method == "GET" and path == "/health":
            self._send_json(200, {"ok": True})
            return

        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        if method == "GET" and path == "/status":
            result = {
                "watcher":     _watcher_status(),
                "queue":       _index_queue.stats(),
                "collections": _collections_status(),
                "scan":        _verify_status_snapshot(),
                "daemon_lock_held": _lock_held(),
                # Compat: clients still inspect these keys; backend is in-process
                # so it's always "ok" once the daemon is up.
                "typesense_ok":            True,
                "typesense_loading":       False,
                "typesense_checked_ago_s": 0.0,
            }
            self._send_json(200, result)
            return

        if method == "POST" and path == "/file-events":
            body   = self._read_body()
            events = body.get("events", [])
            result = _enqueue_file_events(events)
            self._send_json(200, result)
            return

        if method == "POST" and path == "/query-codebase":
            body = self._read_body()
            mode         = body.get("mode", "")
            pattern      = body.get("pattern", "")
            sub          = body.get("sub", "") or ""
            ext          = body.get("ext", "") or ""
            try:
                limit = int(body.get("limit", 50))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "limit must be an integer"})
                return
            if not (1 <= limit <= _QUERY_CODEBASE_MAX_LIMIT):
                self._send_json(400, {"error": f"limit must be between 1 and {_QUERY_CODEBASE_MAX_LIMIT}"})
                return
            include_body = bool(body.get("include_body", False))
            symbol_kind  = str(body.get("symbol_kind", "") or "")
            uses_kind    = str(body.get("uses_kind", "") or "")
            visibility   = str(body.get("visibility", "") or "")
            enclosing_method = str(body.get("enclosing_method", "") or "")
            enclosing_class  = str(body.get("enclosing_class", "") or "")
            exclude_path = str(body.get("exclude_path", "") or "")
            try:
                head_lines_raw = body.get("head_lines", None)
                head_lines = (int(head_lines_raw)
                              if head_lines_raw not in (None, "") else None)
            except (TypeError, ValueError):
                head_lines = None

            if mode not in _EXT_TO_TS_AND_AST:
                self._send_json(400, {"error": f"unknown mode: {mode!r}"})
                return

            ts_mode_flag, ast_mode = _EXT_TO_TS_AND_AST[mode]

            if not ALL_ROOTS:
                self._send_json(400, {"error": "No roots configured."})
                return

            try:
                root = _cfg.get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            collection = root.collection

            backend = _backends.get(collection)
            if backend is None:
                self._send_json(503, {"error": "backend not yet available", "loading": True})
                return

            query_by, weights = resolve_query_params(ts_mode_flag, uses_kind, symbol_kind)
            filter_by         = build_filter_by(ext, sub, exclude_path)

            try:
                ts_result = _search_mod.search(
                    backend,
                    q=pattern,
                    query_by=query_by,
                    weights=weights,
                    per_page=250,
                    num_typos=1,
                    filter_by=filter_by,
                    facet_by="path_segments,language,extension",
                    max_facet_values=200,
                )
            except Exception as e:
                self._send_json(503, {"error": "backend search failed", "detail": str(e)})
                return

            found  = ts_result.get("found", 0)
            hits   = ts_result.get("hits", [])
            facets = ts_result.get("facet_counts", [])

            if found > _QUERY_CODEBASE_MAX_LIMIT:
                self._send_json(200, {
                    "overflow":     True,
                    "found":        found,
                    "hits":         [],
                    "facet_counts": facets,
                })
                return

            file_list:    list[Path] = []
            hit_by_path:  dict[str, dict] = {}
            native_src_root = Path(root.path).resolve()
            for hit in hits:
                rel      = hit["document"].get("relative_path", "")
                abs_path = Path(root.to_local(rel)).resolve()
                try:
                    abs_path.relative_to(native_src_root)
                except ValueError:
                    continue
                if abs_path.is_file():
                    file_list.append(abs_path)
                    hit_by_path[str(abs_path)] = hit

            ast_results = _run_query(ast_mode, pattern, file_list,
                                     include_body=include_body,
                                     symbol_kind=symbol_kind, uses_kind=uses_kind,
                                     visibility=visibility,
                                     head_lines=head_lines,
                                     enclosing_method=enclosing_method,
                                     enclosing_class=enclosing_class)

            response_hits = []
            for ast_item in ast_results:
                file_path = ast_item["file"]
                ts_hit    = hit_by_path.get(file_path)
                if ts_hit is None:
                    continue
                doc = ts_hit.get("document", {})
                response_hits.append({
                    "document": {
                        "id":            doc.get("id", ""),
                        "relative_path": doc.get("relative_path", ""),
                        "filename":      doc.get("filename", ""),
                    },
                    "matches": ast_item["matches"],
                })

            self._send_json(200, {
                "overflow":     False,
                "found":        found,
                "hits":         response_hits,
                "facet_counts": facets,
            })
            return

        if method == "POST" and path == "/management/shutdown":
            self._send_json(200, {"ok": True})
            threading.Thread(target=_shutdown_event.set, daemon=True).start()
            return

        self._send_json(404, {"error": f"not found: {method} {path}"})

    def do_GET(self):   self._dispatch()
    def do_POST(self):  self._dispatch()

    def _dispatch(self):
        import traceback
        try:
            self._handle()
        except Exception:
            tb = traceback.format_exc()
            _crash_log = _RUN_DIR / "daemon_crash.log"
            try:
                with open(_crash_log, "a") as _f:
                    import datetime
                    _f.write(f"\n[{datetime.datetime.now().isoformat()}] {self.command} {self.path}\n{tb}\n")
            except Exception:
                pass
            _LOG.error("CRASH handling %s %s: %s", self.command, self.path, tb)
            try:
                self._send_json(500, {"error": "internal server error",
                                      "detail": tb.splitlines()[-1]})
            except Exception:
                pass


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads      = True
    allow_reuse_address = False


# -- Public API -----------------------------------------------------------------

def _try_acquire_lock() -> bool:
    """Acquire an exclusive OS-level file lock on _DAEMON_LOCK.

    The file handle is stored in _lock_fh and intentionally never closed.
    The OS releases the lock automatically when the process exits (clean,
    killed, or crashed), so a second daemon cannot start until the first
    is truly dead -- not merely mid-shutdown.

    Returns True if the lock was acquired, False if another process holds it.
    """
    global _lock_fh
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(_DAEMON_LOCK, "w+")
        if sys.platform == "win32":
            import msvcrt
            # LK_NBLCK: non-blocking exclusive lock on 1 byte at position 0.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        _lock_fh = fh  # keep handle alive for the process lifetime
        return True
    except (OSError, IOError):
        if fh is not None:
            try:
                fh.close()
            except Exception:
                # Cleanup-only best effort: we're already returning False.
                pass
        return False


def start_daemon() -> bool:
    """Acquire the process lock and start all daemon threads."""
    global _server

    _RUN_DIR.mkdir(parents=True, exist_ok=True)

    # Acquire the exclusive process lock before doing anything else.
    # This is checked before the port bind, so a second daemon that loses
    # the lock race exits cleanly without leaving a ghost entry in session.csv.
    if not _try_acquire_lock():
        return False

    # Only configure CSV logging (which writes the session-start row) after we
    # own the lock -- so every session.csv entry corresponds to a real startup.
    debug_logger.configure(_CSV_DEBUG_DIR, _cfg.csv_debug)

    try:
        _server = _ThreadedHTTPServer(("127.0.0.1", _cfg.port), _Handler)
    except OSError:
        return False

    _DAEMON_PID.write_text(str(os.getpid()))
    _LOG.info("=== STARTED pid=%s port=%s ===", os.getpid(), _cfg.port)

    srv_thread = threading.Thread(target=_server.serve_forever, name="http", daemon=True)
    srv_thread.start()
    _LOG.info("Listening on http://127.0.0.1:%s", _cfg.port)

    _init_stop = threading.Event()
    _init_thread = threading.Thread(
        target=_initialize_async, args=(_init_stop,), name="ts-init", daemon=True
    )
    _init_thread.start()

    return True


def _status_printer(interval: float = 60.0) -> None:
    """Background thread: print a compact status line every `interval` seconds."""
    while not _shutdown_event.wait(timeout=interval):
        try:
            q  = _index_queue.stats()
            w  = _watcher_status()
            cs = _collections_status()
            docs_parts = ", ".join(
                f"{name}={info.get('num_documents') or 0}"
                for name, info in cs.items()
            )
            q_depth  = q.get("depth", 0)
            w_state  = ("watching" if w.get("running") else
                        "paused"   if w.get("paused")  else "stopped")
            q_str = f"  queue={q_depth}" if q_depth else ""
            _LOG.info("status: %s  watcher=%s%s", docs_parts, w_state, q_str)
        except Exception as e:
            _LOG.warning("status-printer error: %s", e)


def run_until_shutdown() -> None:
    def _on_signal(sig, frame):
        _LOG.info("Signal %s -- shutting down...", sig)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except (OSError, ValueError):
        pass

    _printer = threading.Thread(target=_status_printer, name="status-printer", daemon=True)
    _printer.start()

    _run_tray()

    _shutdown_event.wait()

    # Hard-exit timer: if stop_daemon() blocks for any reason (e.g. Tantivy merge
    # threads in a destructor), this fires and kills the process after 10s.
    _hard_exit = threading.Timer(10.0, lambda: os._exit(0))
    _hard_exit.daemon = True
    _hard_exit.start()

    stop_daemon()
    os._exit(0)  # Normal path: exit cleanly once stop_daemon() finishes.


def stop_daemon() -> None:
    """Graceful shutdown: drain+commit the queue, close backends.

    _shutdown_event is already set by the caller; the watcher and init scan
    stop themselves. We just wait for the queue to flush and then close.
    """
    global _server

    t0 = time.monotonic()
    def _elapsed() -> str:
        return f"{time.monotonic() - t0:.1f}s"

    _LOG.info("Draining index queue... (%s)", _elapsed())
    # Queue worker stops pulling new items, runs one final commit to flush
    # already-buffered work, then exits. The hard-exit timer in
    # run_until_shutdown() is the backstop if anything blocks longer.
    _index_queue.stop(timeout=5)
    _LOG.info("Queue drained (%s)", _elapsed())

    for collection, backend in _backends.items():
        _LOG.info("Closing backend %s... (%s)", collection, _elapsed())
        try:
            # quick=True: skip merge-thread wait and uncommitted-work commit.
            # Any buffered work is dropped; the verifier re-indexes on next startup.
            backend.close(quick=True)
            _LOG.info("Closed backend %s (%s)", collection, _elapsed())
        except Exception as e:
            _LOG.warning("backend.close error (%s): %s (%s)", collection, e, _elapsed())
    _backends.clear()

    if _server is not None:
        _LOG.info("Shutting down HTTP server... (%s)", _elapsed())
        _srv_stop = threading.Thread(target=_server.shutdown, daemon=True)
        _srv_stop.start()
        _srv_stop.join(timeout=5)
        _server = None
        _LOG.info("HTTP server down (%s)", _elapsed())

    if _DAEMON_PID.exists():
        _DAEMON_PID.unlink()

    debug_logger.shutdown("stop")
    _LOG.info("Stopped. total=%s", _elapsed())


# -- System-tray icon ----------------------------------------------------------

def _run_tray() -> None:
    """Start a pystray system-tray icon in a background thread.

    Falls back silently if pystray or Pillow is not installed.
    The icon's Stop item sets _shutdown_event, which causes run_until_shutdown
    to proceed with graceful teardown.
    """
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return

    def _make_image() -> Image.Image:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # Lens: blue filled circle with white glass interior
        d.ellipse([4, 4, 40, 40], fill=(0, 120, 215, 255))
        d.ellipse([11, 11, 33, 33], fill=(220, 235, 255, 230))
        # Handle: thick diagonal line extending from lower-right of lens
        d.line([34, 34, 58, 58], fill=(0, 120, 215, 255), width=9)
        return img

    roots_str = ", ".join(
        f"{n}={r.path}" for n, r in ALL_ROOTS.items()
    )
    title = f"Codesearch Daemon  port={_cfg.port}  {roots_str}"

    def _on_stop(icon: pystray.Icon, item: object) -> None:  # noqa: ARG001
        icon.stop()
        _shutdown_event.set()

    icon = pystray.Icon(
        "codesearch",
        _make_image(),
        title,
        menu=pystray.Menu(
            pystray.MenuItem("Stop Daemon", _on_stop),
        ),
    )

    def _tray_thread() -> None:
        try:
            icon.run()
        except Exception as e:
            _LOG.warning("tray icon error: %s", e)

    t = threading.Thread(target=_tray_thread, name="tray", daemon=True)
    t.start()


# -- Logging setup -------------------------------------------------------------

def _setup_logging() -> None:
    """Configure runtime logging to file and optional attached console."""
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _configure_runtime_logger(_RUN_DIR / "daemon.log")


# -- Daemon entry point ---------------------------------------------------------

if __name__ == "__main__":
    # Detach from any console that Windows allocated when spawning python.exe.
    # The daemon is headless (pystray tray icon only); if the process was started
    # via the uv venv shim (which re-spawns python.exe without CREATE_NO_WINDOW),
    # Windows allocates a new console.  FreeConsole() closes that window before it
    # is visible.  Must be called before _setup_logging() opens the log file.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass

    _setup_logging()

    roots_info = "  ".join(f"{n}={r.path}" for n, r in ALL_ROOTS.items())
    _LOG.info("================================================")
    _LOG.info(" Codesearch Daemon  port=%s", _cfg.port)
    _LOG.info(" roots: %s", roots_info)
    _LOG.info("================================================")

    if not start_daemon():
        _LOG.info("Another instance is already running on the port.")
        sys.exit(0)
    run_until_shutdown()
