"""
tsquery_server — cross-platform management server daemon.

Runs under .client-venv (Windows) or indexserver-venv (Linux/WSL).
Runs natively on Windows to avoid the 9P filesystem bottleneck for file
scanning and watching. On Linux/WSL it works as a standalone management server.

Typesense remains in WSL or Docker as a pure storage backend.

Entry points
------------
  start_daemon() -> bool
      Try to bind PORT+1.  Returns True if this process now owns the port,
      False if another instance is already running there.

  run_until_shutdown()
      Block until a shutdown signal is received (SIGTERM, SIGINT, or
      POST /management/shutdown).

  stop_daemon()
      Signal the running daemon to stop (sets the internal shutdown event).

HTTP endpoints:
  GET  /health               → {"ok": true}  (no auth)
  GET  /status               → watcher / queue / syncer / collections / TS health
  POST /check-ready          → check_ready() result
  POST /verify/start         → alias for POST /index/start (resethard=False)
  POST /verify/stop          → cancel running syncer
  POST /index/start          → queue verify/index job
  POST /file-events          → accept file-change notifications
  POST /query-codebase       → Typesense pre-filter + AST post-process
  POST /management/shutdown  → graceful daemon shutdown (used by ts stop)
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

_REPO = Path(__file__).parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from indexserver.config import load_config as _load_config
from indexserver.index_queue import IndexQueue
from indexserver.indexer import get_client, verify_all_schemas
from indexserver.verifier import check_ready, run_verify
from indexserver.watcher import run_watcher

_cfg = _load_config()
# Module-level aliases used throughout this file and patchable by tests
ALL_ROOTS = _cfg.roots

# ── runtime paths ──────────────────────────────────────────────────────────────
_HOME        = Path.home()
_DEFAULT_RUN_DIR = (
    Path(os.environ.get("LOCALAPPDATA", _HOME / "AppData" / "Local")) / "typesense"
    if sys.platform == "win32"
    else _HOME / ".local" / "typesense"
)
_RUN_DIR     = Path(os.environ.get("TYPESENSE_DATA", _DEFAULT_RUN_DIR))
_DAEMON_PID  = _RUN_DIR / "mcp_daemon.pid"
_INDEXER_PID = _RUN_DIR / "indexer.pid"

CHECK_INTERVAL = 30    # heartbeat poll interval (seconds)
FAIL_THRESHOLD = 3     # consecutive failures before restarting Typesense

_QUERY_CODEBASE_MAX_LIMIT = 250

# ── extra config (mode, docker_container) ─────────────────────────────────────
def _load_extra_config() -> dict:
    cfg_path = _REPO / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}

_CFG_EXTRA = _load_extra_config()

# ── thread state ───────────────────────────────────────────────────────────────
_watcher_stop:   threading.Event  = threading.Event()
_watcher_thread: threading.Thread | None = None
_watcher_lock    = threading.Lock()

_sync_thread:   threading.Thread | None = None
_sync_lock      = threading.Lock()
_sync_pending:  list = []
_sync_stop:     threading.Event | None = None
_sync_progress: dict = {}

_schema_status: dict = {}
_ts_client = None
_ts_healthy:      bool  = True
_ts_last_checked: float = 0.0
_ts_initializing: bool  = True
_hb_stop: threading.Event = threading.Event()

_index_queue = IndexQueue(max_file_bytes=_cfg.max_file_bytes)

_server: HTTPServer | None = None
_shutdown_event = threading.Event()

# ── path helpers ───────────────────────────────────────────────────────────────

def _win_to_wsl(p: str) -> str:
    """Convert a Windows path (X:/...) to a WSL mount path (/mnt/x/...)."""
    p = p.replace("\\", "/")
    m = re.match(r"^([a-zA-Z]):(.*)", p)
    if m:
        return f"/mnt/{m.group(1).lower()}{m.group(2)}"
    return p

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


def _resolve_query_paths(raw_files: list) -> list[Path]:
    safe: list[Path] = []
    for file_path in raw_files:
        if not isinstance(file_path, str):
            raise ValueError(f"file path must be a string: {file_path!r}")
        p = file_path.replace("\\", "/")
        matched_root = rel = None
        for root in ALL_ROOTS.values():
            rp = root.path.rstrip("/")
            if p.lower().startswith(rp.lower() + "/"):
                rel = Path(p[len(rp) + 1:])
                matched_root = root
                break

        if matched_root is None or rel is None:
            raise ValueError(f"path does not match any configured root: {file_path!r}")

        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"invalid relative path: {file_path!r}")

        local_root = os.path.realpath(matched_root.path)
        native = os.path.realpath(os.path.join(local_root, str(rel)))
        sep = os.sep
        if sys.platform == "win32":
            if not native.lower().startswith(local_root.lower() + sep):
                raise ValueError(f"path not under a configured root: {file_path!r}")
        else:
            if not native.startswith(local_root + sep):
                raise ValueError(f"path not under a configured root: {file_path!r}")
        safe.append(Path(native))
    return safe


# ── Mode mapping ───────────────────────────────────────────────────────────────

_EXT_TO_TS_AND_AST: dict[str, tuple[str, str]] = {
    "text":         ("text",        "all_refs"),
    "declarations": ("symbols",     "declarations"),
    "calls":        ("calls",       "calls"),
    "implements":   ("implements",  "implements"),
    "uses":         ("uses",        "uses"),
    "casts":        ("casts",       "casts"),
    "attrs":        ("attrs",       "attrs"),
    "accesses_of":  ("accesses_of", "accesses_of"),
    "accesses_on":  ("uses",        "accesses_on"),
    "all_refs":     ("text",        "all_refs"),
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


def _heartbeat_status() -> dict:
    age = (time.monotonic() - _ts_last_checked) if _ts_last_checked else None
    return {
        "typesense_ok":               _ts_healthy,
        "typesense_loading":          _ts_initializing,
        "typesense_checked_ago_s":    round(age, 1) if age is not None else None,
    }


def _collections_status() -> dict:
    collections: dict = {}
    for root in ALL_ROOTS.values():
        schema = _schema_status.get(root.name, {})
        ndocs: int | None = None
        if _ts_client is not None:
            try:
                info = _ts_client.collections[root.collection].retrieve()
                ndocs = info.get("num_documents")
            except Exception:
                pass
        col_live_exists = ndocs is not None
        collections[root.name] = {
            "collection":        root.collection,
            "num_documents":     ndocs,
            "collection_exists": col_live_exists,
            "schema_ok":         col_live_exists and schema.get("ok", False),
            "schema_warnings":   schema.get("warnings", []) if col_live_exists else [],
        }
    return collections

# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts_health() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{_cfg.host}:{_cfg.port}/health", timeout=5
        ) as r:
            return json.loads(r.read()).get("ok", False)
    except urllib.error.HTTPError:
        # 503 "Not Ready or Lagging" — Typesense is alive but under write load.
        # Treat as healthy so the heartbeat does not restart it.
        return True
    except Exception:
        return False


def _restart_typesense() -> None:
    print("[tsquery_server] Restarting Typesense…", flush=True)
    mode      = _CFG_EXTRA.get("mode", "docker")
    venv_py   = str(_HOME / ".local" / "indexserver-venv" / "bin" / "python3")
    service   = str(_REPO / "indexserver" / "service.py")
    if sys.platform == "win32":
        if mode == "wsl":
            repo_wsl = _win_to_wsl(str(_REPO))
            cmd = f"~/.local/indexserver-venv/bin/python3 {repo_wsl}/indexserver/service.py start --typesense-only"
            subprocess.Popen(
                ["wsl.exe", "bash", "-lc", cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            container = _CFG_EXTRA.get("docker_container", "codesearch")
            subprocess.Popen(
                ["docker", "restart", container],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    else:
        # Linux / WSL: call service.py directly
        py = venv_py if os.path.exists(venv_py) else sys.executable
        subprocess.Popen(
            [py, service, "start", "--typesense-only"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


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

# ── Heartbeat ──────────────────────────────────────────────────────────────────

def _heartbeat_loop(stop_event: threading.Event) -> None:
    global _ts_healthy, _ts_last_checked
    failures = 0
    if stop_event.wait(10):
        return

    while not stop_event.is_set():
        if _ts_health():
            if failures:
                print(f"[heartbeat] Recovered after {failures} failure(s).", flush=True)
            failures = 0
            _ts_healthy = True
        else:
            failures += 1
            _ts_healthy = False
            print(f"[heartbeat] Health check failed ({failures}/{FAIL_THRESHOLD}).", flush=True)
            if failures >= FAIL_THRESHOLD:
                _restart_typesense()
                failures = 0

        _ts_last_checked = time.monotonic()

        if _ts_health() and (_watcher_thread is None or not _watcher_thread.is_alive()):
            print("[heartbeat] Watcher thread dead — reviving…", flush=True)
            _start_watcher()

        stop_event.wait(CHECK_INTERVAL)

# ── Syncer ─────────────────────────────────────────────────────────────────────

def _drain_sync_queue() -> None:
    global _sync_stop
    while True:
        with _sync_lock:
            if not _sync_pending:
                break
            job = _sync_pending.pop(0)

        src_root   = job["src_root"]
        collection = job["collection"]
        resethard  = job["resethard"]
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
                resethard=resethard,
                stop_event=stop,
                extensions=extensions,
                on_complete=lambda: None,
                on_progress=lambda p: _sync_progress.update(p),
            )
        except Exception as e:
            print(f"[syncer] ERROR for {collection}: {e}", flush=True)
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
        raw_path = ev.get("path", "").replace("\\", "/")
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

# ── Typesense init ─────────────────────────────────────────────────────────────

def _ts_init_loop(stop_event: threading.Event) -> None:
    global _ts_client, _schema_status, _ts_initializing, _sync_thread

    print("[tsquery_server] Waiting for Typesense to become ready…", flush=True)
    while not stop_event.is_set():
        if _ts_health():
            break
        stop_event.wait(2)

    if stop_event.is_set():
        return

    # /health returning ok doesn't guarantee the collections API is ready yet
    # (Typesense returns 503 "Not Ready or Lagging" while loading RocksDB).
    # Wait until a lightweight collections call succeeds before starting the queue.
    print("[tsquery_server] Waiting for Typesense API to become ready…", flush=True)
    while not stop_event.is_set():
        try:
            get_client(_cfg).collections.retrieve()
            break
        except Exception:
            stop_event.wait(2)

    if stop_event.is_set():
        return

    print("[tsquery_server] Typesense ready — finishing initialization.", flush=True)

    _ts_client = get_client(_cfg)
    _index_queue.start(_ts_client)
    _schema_status = verify_all_schemas(_ts_client, _cfg)

    with _sync_lock:
        for root in ALL_ROOTS.values():
            _sync_pending.append({
                "root_name":  root.name,
                "src_root":   root.path,
                "collection": root.collection,
                "resethard":  False,
                "extensions": root.extensions,
            })
        if _sync_pending:
            _sync_thread = threading.Thread(
                target=_drain_sync_queue, name="syncer", daemon=True
            )
            _sync_thread.start()

    _start_watcher()

    _hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(_hb_stop,), name="heartbeat", daemon=True
    )
    _hb_thread.start()

    _ts_initializing = False
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

        # GET /status
        if method == "GET" and path == "/status":
            result = {
                "watcher":     _watcher_status(),
                "queue":       _index_queue.stats(),
                "syncer":      _syncer_status(),
                "collections": _collections_status(),
                **_heartbeat_status(),
            }
            self._send_json(200, result)
            return

        # POST /check-ready
        if method == "POST" and path == "/check-ready":
            if _ts_initializing:
                self._send_json(200, {"ready": False, "loading": True,
                                      "error": "Typesense is still loading"})
                return
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

        # POST /verify/start alias
        if method == "POST" and path == "/verify/start":
            path = "/index/start"

        # POST /verify/stop
        if method == "POST" and path == "/verify/stop":
            if not (_sync_thread and _sync_thread.is_alive()):
                self._send_json(404, {"error": "no sync job is running"})
                return
            if _sync_stop:
                _sync_stop.set()
            self._send_json(200, {"stopped": True})
            return

        # POST /file-events
        if method == "POST" and path == "/file-events":
            body   = self._read_body()
            events = body.get("events", [])
            result = _enqueue_file_events(events)
            self._send_json(200, result)
            return

        # POST /index/start
        if method == "POST" and path == "/index/start":
            if _ts_initializing:
                self._send_json(503, {"error": "Typesense is still loading", "loading": True})
                return
            body = self._read_body()
            try:
                root = _cfg.get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            resethard = bool(body.get("resethard", False))

            with _sync_lock:
                job = {"root_name": root.name, "src_root": root.path,
                       "collection": root.collection, "resethard": resethard,
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

        # POST /query-codebase
        if method == "POST" and path == "/query-codebase":
            if _ts_initializing:
                self._send_json(503, {"error": "Typesense is still loading", "loading": True})
                return
            body    = self._read_body()
            mode         = body.get("mode", "")
            pattern      = body.get("pattern", "")
            sub          = body.get("sub", "") or None
            ext          = body.get("ext", "") or None
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

            _search_spec = _ilu.spec_from_file_location(
                "scripts.search",
                str(_REPO / "scripts" / "search.py"),
            )
            _search_mod = _ilu.module_from_spec(_search_spec)
            _search_spec.loader.exec_module(_search_mod)

            import io as _io
            import sys as _sys
            _buf = _io.StringIO()
            _old_stdout = _sys.stdout
            _sys.stdout = _buf
            try:
                ts_result, _ = _search_mod.search(
                    query        = pattern,
                    ext          = ext,
                    sub          = sub,
                    limit        = 250,
                    symbols_only  = (ts_mode_flag == "symbols"),
                    implements    = (ts_mode_flag == "implements"),
                    calls         = (ts_mode_flag == "calls"),
                    uses          = (ts_mode_flag == "uses"),
                    attrs         = (ts_mode_flag == "attrs"),
                    casts         = (ts_mode_flag == "casts"),
                    accesses_of   = (ts_mode_flag == "accesses_of"),
                    collection   = collection,
                    symbol_kind  = symbol_kind,
                    uses_kind    = uses_kind,
                )
            except SystemExit:
                detail = _buf.getvalue().strip()
                self._send_json(503, {"error": "Typesense search failed", "detail": detail})
                return
            finally:
                _sys.stdout = _old_stdout

            found  = ts_result.get("found", 0)
            hits   = ts_result.get("hits", [])
            facets = ts_result.get("facet_counts", [])

            ast_hits     = hits[:limit]
            pending_hits = hits[limit:]

            file_list:    list[Path] = []
            hit_by_path:  dict[str, dict] = {}
            native_src_root = Path(root.path).resolve()
            for hit in ast_hits:
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
                        "subsystem":     doc.get("subsystem", ""),
                        "filename":      doc.get("filename", ""),
                    },
                    "matches":      ast_item["matches"],
                    "ast_expanded": True,
                })

            for hit in pending_hits:
                doc = hit.get("document", {})
                response_hits.append({
                    "document": {
                        "id":            doc.get("id", ""),
                        "relative_path": root.to_external(doc.get("relative_path", "")),
                        "subsystem":     doc.get("subsystem", ""),
                        "filename":      doc.get("filename", ""),
                    },
                    "matches":      [],
                    "ast_expanded": False,
                })

            self._send_json(200, {
                "overflow":     found > len(hits),
                "found":        found,
                "hits":         response_hits,
                "facet_counts": facets,
            })
            return

        # POST /management/shutdown
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
    daemon_threads    = True
    allow_reuse_address = False   # Don't set SO_REUSEADDR; let bind() fail fast if port is taken

# ── Public API ─────────────────────────────────────────────────────────────────

def start_daemon() -> bool:
    """Try to bind PORT+1 and start all daemon threads.

    Returns True if this call owns the port (daemon started),
    False if another instance is already running there.
    """
    global _server

    _RUN_DIR.mkdir(parents=True, exist_ok=True)

    _sync_progress.clear()

    try:
        _server = _ThreadedHTTPServer(("127.0.0.1", _cfg.api_port), _Handler)
    except OSError:
        return False   # already bound by another instance

    _DAEMON_PID.write_text(str(os.getpid()))
    print(f"[tsquery_server] === STARTED pid={os.getpid()} port={_cfg.api_port} ===", flush=True)

    srv_thread = threading.Thread(target=_server.serve_forever, name="http", daemon=True)
    srv_thread.start()
    print(f"[tsquery_server] Listening on http://127.0.0.1:{_cfg.api_port}", flush=True)

    _init_stop = threading.Event()
    _init_thread = threading.Thread(
        target=_ts_init_loop, args=(_init_stop,), name="ts-init", daemon=True
    )
    _init_thread.start()

    return True


def run_until_shutdown() -> None:
    """Block until shutdown is signalled (SIGTERM, SIGINT, or POST /management/shutdown)."""
    def _on_signal(sig, frame):
        print(f"[tsquery_server] Signal {sig} — shutting down…", flush=True)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except (OSError, ValueError):
        pass  # SIGINT may not be available in all contexts

    _shutdown_event.wait()
    stop_daemon()


def stop_daemon() -> None:
    """Gracefully stop all threads and the HTTP server."""
    global _server

    _hb_stop.set()
    _watcher_stop.set()

    if _sync_thread and _sync_thread.is_alive():
        if _sync_stop:
            _sync_stop.set()
        _sync_thread.join(timeout=5)

    if _ts_client is not None:
        _index_queue.stop(timeout=5)

    if _server is not None:
        _srv_stop = threading.Thread(target=_server.shutdown, daemon=True)
        _srv_stop.start()
        _srv_stop.join(timeout=5)
        _server = None

    if _DAEMON_PID.exists():
        _DAEMON_PID.unlink()

    print("[tsquery_server] Stopped.", flush=True)


# ── Daemon entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not start_daemon():
        print("[tsquery_server] Another instance is already running on the port.", flush=True)
        sys.exit(0)
    run_until_shutdown()
