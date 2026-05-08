"""
tsquery_server — cross-platform management server daemon.

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
  GET  /health               → {"ok": true}  (no auth)
  GET  /status               → watcher / queue / syncer / collections
  POST /check-ready          → check_ready() result
  POST /verify/start         → queue a verify/repair job
  POST /verify/stop          → cancel running syncer
  POST /file-events          → accept file-change notifications
  POST /query-codebase       → backend search + AST post-process
  POST /management/shutdown  → graceful daemon shutdown (used by ts stop)
"""

from __future__ import annotations

import json
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

from indexserver.config import load_config as _load_config, normalize_path
from indexserver.index_queue import IndexQueue
from indexserver.indexer import ensure_backend
from indexserver.verifier import check_ready, run_verify
from indexserver.watcher import run_watcher
from indexserver import search as _search_mod

_cfg = _load_config()
ALL_ROOTS = _cfg.roots

# ── runtime paths ──────────────────────────────────────────────────────────────
_HOME = Path.home()
_DEFAULT_RUN_DIR = (
    Path(os.environ.get("LOCALAPPDATA", _HOME / "AppData" / "Local")) / "tscodesearch"
    if sys.platform == "win32"
    else _HOME / ".local" / "tscodesearch"
)
_RUN_DIR     = Path(os.environ.get("TSCODESEARCH_DATA", _DEFAULT_RUN_DIR))
_DAEMON_PID  = _RUN_DIR / "daemon.pid"
_INDEXER_PID = _RUN_DIR / "indexer.pid"

_QUERY_CODEBASE_MAX_LIMIT = 250

# ── thread state ───────────────────────────────────────────────────────────────
_watcher_stop:   threading.Event  = threading.Event()
_watcher_thread: threading.Thread | None = None
_watcher_lock    = threading.Lock()

_sync_thread:   threading.Thread | None = None
_sync_lock      = threading.Lock()
_sync_pending:  list = []
_sync_stop:     threading.Event | None = None
_sync_progress: dict = {}

_synced_roots: dict[str, str] = {}   # root_name → ISO timestamp of last successful sync

# Backends keyed by collection name. The daemon owns the writer for each.
_backends: dict[str, object] = {}

_index_queue = IndexQueue(max_file_bytes=_cfg.max_file_bytes)

_server: HTTPServer | None = None
_shutdown_event = threading.Event()


# ── Tree-sitter query helper ───────────────────────────────────────────────────

_query_module = None

def _get_query_module():
    global _query_module
    if _query_module is None:
        import query.dispatch as _q
        _query_module = _q
    return _query_module


def _run_query(mode: str, pattern: str, files: list[Path],
               include_body: bool = False, symbol_kind: str = "",
               uses_kind: str = "") -> list:
    _q = _get_query_module()
    results = []
    for path in files:
        native = path.resolve()
        ext = native.suffix.lower()
        try:
            src_bytes = native.read_bytes()
        except OSError as e:
            print(f"ERROR reading {native}: {e}", file=sys.stderr)
            continue
        matches = _q.query_file(src_bytes, ext, mode, pattern,
                                include_body=include_body,
                                symbol_kind=symbol_kind,
                                uses_kind=uses_kind)
        if matches:
            results.append({"file": str(native), "matches": matches})
    return results


# ── Mode mapping ───────────────────────────────────────────────────────────────

_EXT_TO_TS_AND_AST: dict[str, tuple[str, str]] = {
    "declarations": ("symbols",     "declarations"),
    "calls":        ("calls",       "calls"),
    "implements":   ("implements",  "implements"),
    "uses":         ("uses",        "uses"),
    "casts":        ("casts",       "casts"),
    "attrs":        ("attrs",       "attrs"),
    "accesses_of":  ("accesses_of", "accesses_of"),
    "accesses_on":  ("uses",        "accesses_on"),
    "all_refs":     ("all_refs",    "all_refs"),
}


# ── Per-component status ───────────────────────────────────────────────────────

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


def _syncer_status() -> dict:
    running = bool(_sync_thread and _sync_thread.is_alive())
    result: dict = {
        "running": running,
        "pending": len(_sync_pending),
    }
    if _sync_progress:
        result["progress"] = _sync_progress
    return result


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
        synced_at = _synced_roots.get(root.name)
        collections[root.name] = {
            "collection":        root.collection,
            "num_documents":     ndocs,
            "buffered":          buffered,
            "collection_exists": col_live_exists,
            "schema_ok":         col_live_exists,
            "schema_warnings":   [],
            "synced":            bool(synced_at),
            "synced_at":         synced_at,
        }
    return collections


# ── Search resolver: query_by and weights for each mode ────────────────────────

def _resolve_query_params(ts_mode_flag: str, uses_kind: str, symbol_kind: str
                         ) -> tuple[str, str]:
    """Return (query_by, weights) for the given mode flag."""
    if ts_mode_flag == "implements":
        return "base_types,class_names,filename", "4,3,2"
    if ts_mode_flag == "calls":
        return "call_sites,filename", "4,2"
    if ts_mode_flag == "uses":
        k = (uses_kind or "all").lower().strip()
        if k == "field":   return "field_types,filename", "4,2"
        if k == "param":   return "param_types,filename", "4,2"
        if k == "return":  return "return_types,filename", "4,2"
        if k == "cast":    return "cast_types,filename", "4,2"
        if k == "base":    return "base_types,class_names,filename", "4,3,2"
        if k == "locals":  return "local_types,filename", "4,2"
        return "type_refs,cast_types,filename", "4,3,2"
    if ts_mode_flag == "attrs":       return "attr_names,filename", "4,2"
    if ts_mode_flag == "casts":       return "cast_types,filename", "4,2"
    if ts_mode_flag == "accesses_of": return "member_accesses,filename", "4,2"
    if ts_mode_flag == "symbols":
        from query.cs import symbol_kind_query_by
        narrowed = symbol_kind_query_by(symbol_kind or "")
        return (narrowed or "class_names,method_names,filename"), "4,3,2"
    if ts_mode_flag == "all_refs":
        return "filename,class_names,method_names,tokens", "5,4,4,1"
    # Should never reach here — _EXT_TO_TS_AND_AST gates the mode flags above.
    return "filename,class_names,method_names,tokens", "5,4,4,1"


def _build_filter_by(ext: str, sub: str, exclude_path: str) -> str:
    parts = []
    if ext:
        exts = {e.lstrip(".") for e in ext.split(",") if e.strip()}
        _CPP_SRC = {"cpp", "cc", "cxx", "c"}
        _CPP_HDR = {"h", "hpp", "hxx"}
        if exts & _CPP_SRC:
            exts |= _CPP_HDR
        if len(exts) == 1:
            parts.append(f"extension:={next(iter(exts))}")
        elif exts:
            parts.append(f"extension:=[{','.join(sorted(exts))}]")
    if sub:
        included = [normalize_path(p).strip("/") for p in sub.split(",")]
        included = [p for p in included if p]
        if len(included) == 1:
            parts.append(f"path_segments:={included[0]}")
        elif included:
            parts.append(f"path_segments:=[{','.join(included)}]")
    if exclude_path:
        excluded = [normalize_path(p).strip("/") for p in exclude_path.split(",")]
        excluded = [p for p in excluded if p]
        if len(excluded) == 1:
            parts.append(f"path_segments:!={excluded[0]}")
        elif excluded:
            parts.append(f"path_segments:!=[{','.join(excluded)}]")
    return " && ".join(parts)


# ── Watcher ────────────────────────────────────────────────────────────────────

def _start_watcher() -> None:
    global _watcher_thread, _watcher_stop
    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return
        _watcher_stop = threading.Event()
        _watcher_thread = threading.Thread(
            target=run_watcher,
            args=(_cfg,),
            kwargs={"stop_event": _watcher_stop, "queue": _index_queue},
            name="watcher",
            daemon=True,
        )
        _watcher_thread.start()
        print("[tsquery_server] Watcher thread started.", flush=True)


# ── Syncer ─────────────────────────────────────────────────────────────────────

def _drain_sync_queue() -> None:
    global _sync_stop

    while True:
        with _sync_lock:
            if not _sync_pending:
                break
            job = _sync_pending.pop(0)

        root_name  = job.get("root_name", "")
        src_root   = job["src_root"]
        collection = job["collection"]
        extensions = job.get("extensions")

        stop = threading.Event()
        _sync_stop = stop

        _sync_progress.clear()
        _INDEXER_PID.write_text(str(os.getpid()))
        try:
            run_verify(
                _cfg,
                src_root=src_root,
                collection=collection,
                queue=_index_queue,
                delete_orphans=True,
                stop_event=stop,
                extensions=extensions,
                on_complete=lambda: None,
                on_progress=lambda p: _sync_progress.update(p),
                backend=_backends.get(collection),
            )
        except Exception as e:
            print(f"[syncer] ERROR for {collection}: {e}", flush=True)
        else:
            if _sync_progress.get("status") != "cancelled":
                _synced_roots[root_name] = time.strftime("%Y-%m-%dT%H:%M:%S")
        finally:
            if _INDEXER_PID.exists():
                _INDEXER_PID.unlink()

    _sync_stop = None


# ── File-event handler ─────────────────────────────────────────────────────────

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
        if coll is None:
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


# ── Daemon initialization ──────────────────────────────────────────────────────

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
            print(
                f"[tsquery_server] Opened backend {root.collection} "
                f"({ndocs:,} docs on disk) at {root.index_dir}",
                flush=True,
            )
        except Exception as e:
            print(f"[tsquery_server] WARNING: could not open backend {root.collection}: {e}", flush=True)


def _initialize_async(stop_event: threading.Event) -> None:
    """Open backends, start the queue, queue the initial sync, start the watcher."""
    global _sync_thread

    _init_backends()
    _index_queue.start(lambda c: _backends.get(c))

    with _sync_lock:
        for root in ALL_ROOTS.values():
            _sync_pending.append({
                "root_name":  root.name,
                "src_root":   root.path,
                "collection": root.collection,
                "extensions": root.extensions,
            })
        if _sync_pending:
            _sync_thread = threading.Thread(
                target=_drain_sync_queue, name="syncer", daemon=True
            )
            _sync_thread.start()

    _start_watcher()
    print("[tsquery_server] Initialization complete.", flush=True)


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _auth(self) -> bool:
        return self.headers.get("X-TYPESENSE-API-KEY") == _cfg.api_key

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
        global _sync_thread

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
                "syncer":      _syncer_status(),
                "collections": _collections_status(),
                # Compat: clients still inspect these keys; backend is in-process
                # so it's always "ok" once the daemon is up.
                "typesense_ok":            True,
                "typesense_loading":       False,
                "typesense_checked_ago_s": 0.0,
            }
            self._send_json(200, result)
            return

        if method == "POST" and path == "/check-ready":
            body = self._read_body()
            try:
                root = _cfg.get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            result = check_ready(_cfg, src_root=root.path, collection=root.collection,
                                 extensions=root.extensions)
            self._send_json(200, result)
            return

        if method == "POST" and path == "/verify/stop":
            if not (_sync_thread and _sync_thread.is_alive()):
                self._send_json(404, {"error": "no sync job is running"})
                return
            if _sync_stop:
                _sync_stop.set()
            self._send_json(200, {"stopped": True})
            return

        if method == "POST" and path == "/file-events":
            body   = self._read_body()
            events = body.get("events", [])
            result = _enqueue_file_events(events)
            self._send_json(200, result)
            return

        if method == "POST" and path == "/verify/start":
            body = self._read_body()
            try:
                root = _cfg.get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return

            with _sync_lock:
                job = {"root_name": root.name, "src_root": root.path,
                       "collection": root.collection,
                       "extensions": root.extensions}
                _sync_pending.append(job)
                queued_pos = len(_sync_pending)
                if not (_sync_thread and _sync_thread.is_alive()):
                    _sync_thread = threading.Thread(
                        target=_drain_sync_queue, name="syncer", daemon=True
                    )
                    _sync_thread.start()
                    queued_pos = 0

            self._send_json(200, {
                "started":    queued_pos == 0,
                "queued":     queued_pos > 0,
                "position":   queued_pos,
                "collection": root.collection,
                "src_root":   root.path,
            })
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
            exclude_path = str(body.get("exclude_path", "") or "")

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

            query_by, weights = _resolve_query_params(ts_mode_flag, uses_kind, symbol_kind)
            filter_by         = _build_filter_by(ext, sub, exclude_path)

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
                                     symbol_kind=symbol_kind, uses_kind=uses_kind)

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
                        "relative_path": root.to_external(doc.get("relative_path", "")),
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
            print(f"[tsquery_server] CRASH: {tb}", flush=True)
            try:
                self._send_json(500, {"error": "internal server error",
                                      "detail": tb.splitlines()[-1]})
            except Exception:
                pass


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads      = True
    allow_reuse_address = False


# ── Public API ─────────────────────────────────────────────────────────────────

def start_daemon() -> bool:
    """Try to bind PORT and start all daemon threads."""
    global _server

    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _sync_progress.clear()

    try:
        _server = _ThreadedHTTPServer(("127.0.0.1", _cfg.port), _Handler)
    except OSError:
        return False

    _DAEMON_PID.write_text(str(os.getpid()))
    print(f"[tsquery_server] === STARTED pid={os.getpid()} port={_cfg.port} ===", flush=True)

    srv_thread = threading.Thread(target=_server.serve_forever, name="http", daemon=True)
    srv_thread.start()
    print(f"[tsquery_server] Listening on http://127.0.0.1:{_cfg.port}", flush=True)

    _init_stop = threading.Event()
    _init_thread = threading.Thread(
        target=_initialize_async, args=(_init_stop,), name="ts-init", daemon=True
    )
    _init_thread.start()

    return True


def run_until_shutdown() -> None:
    def _on_signal(sig, frame):
        print(f"[tsquery_server] Signal {sig} — shutting down…", flush=True)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except (OSError, ValueError):
        pass

    _shutdown_event.wait()

    # Hard-exit timer: if stop_daemon() blocks for any reason (e.g. Tantivy merge
    # threads in a destructor), this fires and kills the process after 10s.
    _hard_exit = threading.Timer(10.0, lambda: os._exit(0))
    _hard_exit.daemon = True
    _hard_exit.start()

    stop_daemon()
    os._exit(0)  # Normal path: exit cleanly once stop_daemon() finishes.


def stop_daemon() -> None:
    """Graceful shutdown: stop producers, drain+commit the queue, close backends.

    The order matters: the syncer feeds the queue, the queue worker commits to
    the backends. We stop them in that order so each layer finishes flushing
    before the next layer goes away.
    """
    global _server

    t0 = time.monotonic()
    def _elapsed() -> str:
        return f"{time.monotonic() - t0:.1f}s"

    print("[tsquery_server] Stopping — signalling watcher…", flush=True)
    _watcher_stop.set()

    if _sync_thread and _sync_thread.is_alive():
        print("[tsquery_server] Waiting for syncer thread…", flush=True)
        if _sync_stop:
            _sync_stop.set()
        _sync_thread.join(timeout=30)
        if _sync_thread.is_alive():
            print(f"[tsquery_server] WARNING: syncer still alive after 30s ({_elapsed()})", flush=True)
        else:
            print(f"[tsquery_server] Syncer done ({_elapsed()})", flush=True)

    print(f"[tsquery_server] Draining index queue… ({_elapsed()})", flush=True)
    # Queue worker stops pulling new items, runs one final commit to flush
    # already-buffered work, then exits. The hard-exit timer in
    # run_until_shutdown() is the backstop if anything blocks longer.
    _index_queue.stop(timeout=5)
    print(f"[tsquery_server] Queue drained ({_elapsed()})", flush=True)

    for collection, backend in _backends.items():
        print(f"[tsquery_server] Closing backend {collection}… ({_elapsed()})", flush=True)
        try:
            # quick=True: skip merge-thread wait and uncommitted-work commit.
            # Any buffered work is dropped; the verifier re-indexes on next startup.
            backend.close(quick=True)
            print(
                f"[tsquery_server] Closed backend {collection} ({_elapsed()})",
                flush=True,
            )
        except Exception as e:
            print(f"[tsquery_server] backend.close error ({collection}): {e} ({_elapsed()})", flush=True)
    _backends.clear()

    if _server is not None:
        print(f"[tsquery_server] Shutting down HTTP server… ({_elapsed()})", flush=True)
        _srv_stop = threading.Thread(target=_server.shutdown, daemon=True)
        _srv_stop.start()
        _srv_stop.join(timeout=5)
        _server = None
        print(f"[tsquery_server] HTTP server down ({_elapsed()})", flush=True)

    if _DAEMON_PID.exists():
        _DAEMON_PID.unlink()

    print(f"[tsquery_server] Stopped. total={_elapsed()}", flush=True)


# ── Daemon entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not start_daemon():
        print("[tsquery_server] Another instance is already running on the port.", flush=True)
        sys.exit(0)
    run_until_shutdown()
