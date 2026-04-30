"""
Indexserver — single Python process for the entire indexserver stack.

Combines in one process:
  - HTTP management API   port PORT+1, authenticated with the same API key
  - File watcher          thread: PollingObserver on all configured roots
  - Heartbeat watchdog    thread: checks Typesense health every 30 s
  - Syncer               on-demand thread: run_verify() via POST /index/start (or /verify/start alias)

Started by: ts start
Stopped by: ts stop (SIGTERM → graceful shutdown of all threads)

Endpoints (all require X-TYPESENSE-API-KEY header):
  GET  /health              → {"ok": true}
  GET  /status              → watcher stats, syncer state (includes progress)
  POST /check-ready         → body {"root": "default"} → check_ready() result
  POST /verify/start        → alias for POST /index/start (no resethard)
  POST /verify/stop         → cancel running syncer
  POST /index/start         → body {"root": "default", "resethard": false}
  POST /watcher/pause       → stop PollingObserver thread; heartbeat won't auto-revive it
  POST /watcher/resume      → restart PollingObserver thread (clears pause flag)
  POST /file-events         → body {"events": [{"path": "Q:/src/foo.cs", "action": "upsert"|"delete"}]}
                              Receives real-time change notifications from the Windows watcher
                              (VS Code extension). Paths are Windows drive-letter style;
                              converted to native WSL/Linux paths before indexing.
  POST /query               → body {"mode": "calls", "pattern": "MethodName", "files": ["/abs/path.cs"]}
                              Run a tree-sitter C# AST query against the given files.
                              Returns {"results": [{"file": path, "matches": [{"line": N, "text": "..."}]}]}
                              Modes: text, declarations, calls, implements, uses, casts, attrs,
                                     accesses_of, accesses_on, all_refs, classes, methods, fields,
                                     usings, params
                              uses accepts uses_kind: field, param, return, cast, base (default: all)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)


from indexserver.config import (
    API_KEY, API_PORT, PORT, HOST, ROOTS, HOST_ROOTS, get_root,
    EXCLUDE_DIRS, to_native_path, collection_for_root, extensions_for_root,
)
from indexserver.index_queue import IndexQueue
from indexserver.indexer import get_client, verify_all_schemas
from indexserver.verifier import check_ready, run_verify
from indexserver.watcher import run_watcher

# ── runtime paths ──────────────────────────────────────────────────────────────
_HOME          = Path.home()
_RUN_DIR       = Path(os.environ.get("TYPESENSE_DATA", _HOME / ".local" / "typesense"))
_API_PID       = _RUN_DIR / "api.pid"
_INDEXER_PID   = _RUN_DIR / "indexer.pid"   # set while verifier is running
_PROGRESS_FILE = _RUN_DIR / "verifier_progress.json"
_WATCHER_STATS = _RUN_DIR / "watcher_stats.json"
_THIS_DIR      = Path(__file__).parent
_VENV_PY_PATH  = _HOME / ".local" / "indexserver-venv" / "bin" / "python3"
_VENV_PY       = str(_VENV_PY_PATH) if _VENV_PY_PATH.exists() else sys.executable
_SERVER_PY     = str(_THIS_DIR / "start_server.py")
_ENTRYPOINT    = str(_THIS_DIR.parent / "scripts" / "entrypoint.sh")


CHECK_INTERVAL = 30    # heartbeat poll interval (seconds)
FAIL_THRESHOLD = 3     # consecutive failures before restarting Typesense

# ── thread state ───────────────────────────────────────────────────────────────
_watcher_stop  = threading.Event()
_watcher_thread: threading.Thread | None = None
_watcher_lock  = threading.Lock()

# Unified sync thread — runs verify/index jobs sequentially.
# Both POST /index/start and POST /verify/start feed this queue.
_sync_thread:  threading.Thread | None = None
_sync_lock     = threading.Lock()
_sync_pending: list = []   # jobs: [{root_name,src_root,collection,resethard,host_root}]
_sync_stop:    threading.Event | None = None   # stop event for the running job

# Schema validation results cached at startup by verify_all_schemas().
# Shape: {root_name: {"ok": bool, "warnings": [...], "collection": str}}
_schema_status: dict = {}

# Typesense client, set in run() and used by /status for live doc counts.
_ts_client = None

# Last known Typesense health; updated every heartbeat interval.
_ts_healthy: bool = True

# True from startup until the Typesense client is ready and all init is done.
_ts_initializing: bool = True

# Stop event for the heartbeat thread; set here so shutdown can reach it even
# if the init thread hasn't started the heartbeat yet.
_hb_stop: threading.Event = threading.Event()

# Set to True by POST /watcher/pause (Windows watcher running — polling not needed).
# Suppresses heartbeat auto-revival of the watcher thread.
_watcher_paused = False

# ── shared index queue ─────────────────────────────────────────────────────────
_index_queue = IndexQueue()

# ── Windows file-event handler (enqueues into _index_queue) ───────────────────

def _enqueue_file_events(events: list) -> dict:
    """Enqueue file-change notifications from the Windows watcher.

    Each event: {"path": "C:/myproject/src/foo.cs", "action": "upsert"|"delete"}
    Paths are Windows drive-letter style; converted to native paths before use.
    Filtering (extension, excluded dirs) is applied before enqueuing.
    """
    root_map = [
        (to_native_path(win_root).rstrip("/"), collection_for_root(name), extensions_for_root(name))
        for name, win_root in ROOTS.items()
    ]

    n_new = n_dedup = 0
    for ev in events:
        raw_path = ev.get("path", "").replace("\\", "/")
        action   = ev.get("action", "upsert")
        ext      = os.path.splitext(raw_path)[1].lower()

        native_path = to_native_path(raw_path)

        coll = native_root = root_exts = None
        for nr, c, exts in root_map:
            if native_path.startswith(nr + "/"):
                native_root, coll, root_exts = nr, c, exts
                break
        if coll is None:
            continue

        if ext not in root_exts:
            continue

        rel   = native_path[len(native_root) + 1:]
        parts = rel.split("/")
        if any(p in EXCLUDE_DIRS or p.startswith(".") for p in parts[:-1]):
            continue

        if _index_queue.enqueue(native_path, rel, coll, action):
            n_new += 1
        else:
            n_dedup += 1

    return {"queued": n_new, "deduped": n_dedup}


# ── In-process indexer thread ──────────────────────────────────────────────────

def _drain_sync_queue() -> None:
    """Run all pending sync jobs sequentially (the _sync_thread target).

    Each job calls run_verify() with the shared IndexQueue. The verifier
    enqueues upserts, deletes orphans synchronously, then places a fence
    so progress is updated only after all files reach Typesense.
    """
    global _sync_stop
    while True:
        with _sync_lock:
            if not _sync_pending:
                break
            job = _sync_pending.pop(0)

        src_root   = job["src_root"]
        collection = job["collection"]
        resethard  = job["resethard"]
        host_root  = job["host_root"]
        extensions = job.get("extensions")

        if host_root:
            _index_queue.register_host_root(collection, host_root)

        stop = threading.Event()
        _sync_stop = stop

        _INDEXER_PID.write_text(str(os.getpid()))
        try:
            run_verify(
                src_root=src_root,
                collection=collection,
                queue=_index_queue,
                delete_orphans=True,
                resethard=resethard,
                stop_event=stop,
                extensions=extensions,
                on_complete=lambda: None,  # places a fence so progress reaches "complete" after queue drains
            )
        except Exception as e:
            print(f"[syncer] ERROR for {collection}: {e}", flush=True)
        finally:
            if _INDEXER_PID.exists():
                _INDEXER_PID.unlink()

    _sync_stop = None


# ── helpers ────────────────────────────────────────────────────────────────────

def _ts_health() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{PORT}/health", timeout=5
        ) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def _restart_typesense() -> None:
    print("[heartbeat] Restarting Typesense server…", flush=True)
    subprocess.run([_VENV_PY, _SERVER_PY, "--stop"], capture_output=True)
    time.sleep(2)
    env = os.environ.copy()
    _config_file = env.get("CODESEARCH_CONFIG") or str(_THIS_DIR.parent / "config.json")
    env.update({
        "TYPESENSE_DATA":      str(_RUN_DIR),
        "CONFIG_FILE":         _config_file,
        "APP_ROOT":            str(_THIS_DIR.parent),
        "PYTHON3":             _VENV_PY,
        "PYTHONPATH":          str(_THIS_DIR.parent),
        "CODESEARCH_API_HOST": "127.0.0.1",
    })
    result = subprocess.run(["bash", _ENTRYPOINT, "--background", "--disown"], env=env,
                            capture_output=True, text=True)
    if result.returncode == 0:
        print("[heartbeat] Server restarted OK.", flush=True)
    else:
        print(f"[heartbeat] Server restart FAILED: {result.stderr[:200]}", flush=True)


def _start_watcher() -> None:
    """Start the watcher thread (or restart it if it has died)."""
    global _watcher_thread, _watcher_stop
    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return
        _watcher_stop = threading.Event()
        _watcher_thread = threading.Thread(
            target=run_watcher,
            kwargs={"stop_event": _watcher_stop, "queue": _index_queue},
            name="watcher",
            daemon=True,
        )
        _watcher_thread.start()
        print("[api] Watcher thread started.", flush=True)


# ── heartbeat loop (runs as a thread) ─────────────────────────────────────────

def _heartbeat_loop(stop_event: threading.Event) -> None:
    global _ts_healthy
    failures = 0
    # Give Typesense a moment to be ready before first check
    if stop_event.wait(10):
        return

    while not stop_event.is_set():
        if _ts_health():
            if failures:
                print(f"[heartbeat] Server recovered after {failures} failure(s).", flush=True)
            failures = 0
            _ts_healthy = True
        else:
            failures += 1
            _ts_healthy = False
            print(f"[heartbeat] Health check failed ({failures}/{FAIL_THRESHOLD}).", flush=True)
            if failures >= FAIL_THRESHOLD:
                _restart_typesense()
                failures = 0

        # Revive watcher thread if it has died and Typesense is healthy.
        # Skip if intentionally paused (Windows watcher is handling events).
        if not _watcher_paused and _ts_health() and (_watcher_thread is None or not _watcher_thread.is_alive()):
            print("[heartbeat] Watcher thread dead — reviving…", flush=True)
            _start_watcher()

        stop_event.wait(CHECK_INTERVAL)


# ── Tree-sitter query helper ───────────────────────────────────────────────────

_query_module = None

def _get_query_module():
    global _query_module
    if _query_module is None:
        import query.dispatch as _q  # src/query/dispatch.py — on sys.path via _base above
        _query_module = _q
    return _query_module


def _run_query(mode: str, pattern: str, files: list, include_body: bool = False, symbol_kind: str = "", uses_kind: str = "") -> list:
    """Run a tree-sitter AST query against a list of normalized absolute file paths.

    Callers are responsible for normalizing paths with os.path.realpath and
    verifying they fall under a configured root before passing them here.

    Returns a list of {"file": path, "matches": [{"line": N, "text": "..."}]}
    where line is 1-indexed.  Only files with at least one match are included.
    """
    _q = _get_query_module()
    results = []
    for path in files:
        native = os.path.realpath(path)
        ext = os.path.splitext(native)[1].lower()
        try:
            with open(native, "rb") as _f:
                src_bytes = _f.read()
        except OSError as e:
            print(f"ERROR reading {native}: {e}", file=sys.stderr)
            continue
        matches = _q.query_file(src_bytes, ext, mode, pattern,
                                include_body=include_body,
                                symbol_kind=symbol_kind,
                                uses_kind=uses_kind)
        if matches:
            results.append({"file": native, "matches": matches})
    return results


def _resolve_query_paths(raw_files: list) -> list[str]:
    """Translate and validate a list of raw file paths from a client request.

    1. Translates Windows host paths to native paths via HOST_ROOTS → ROOTS.
    2. Normalizes each path with os.path.realpath.
    3. Rejects any path that doesn't start with a configured root.

    Returns the list of safe, normalized absolute paths.
    Raises ValueError if any path falls outside every configured root.
    """
    allowed_roots = [os.path.realpath(to_native_path(r)) for r in ROOTS.values() if r]
    safe: list[str] = []
    for file_path in raw_files:
        # Translate Windows host path (e.g. C:/myproject/src/Foo.cs) to the
        # server-local path (/mnt/c/myproject/src/Foo.cs) using HOST_ROOTS → ROOTS.
        resolved = file_path.replace("\\", "/")
        for name, host_root in HOST_ROOTS.items():
            hr = host_root.replace("\\", "/").rstrip("/")
            if resolved.lower().startswith(hr.lower() + "/") or resolved.lower() == hr.lower():
                rel = resolved[len(hr):]  # includes leading /
                resolved = ROOTS[name].rstrip("/") + rel
                break
        native = Path(os.path.realpath(to_native_path(resolved)))
        # Guard: normalized path must be relative to a known configured root.
        for r in allowed_roots:
            if native.is_relative_to(r):
                safe.append(str(native))
                break
        else:
            raise ValueError(f"path not under a configured root: {file_path!r}")
    return safe


# ── Mode mapping: extension mode key → (Typesense mode flag, AST mode) ─────────

_EXT_TO_TS_AND_AST: dict[str, tuple[str, str]] = {
    # Primary modes
    "text":            ("text",       "all_refs"),
    "declarations":    ("symbols",    "declarations"),
    "calls":           ("calls",      "calls"),
    "implements":      ("implements", "implements"),
    "uses":            ("uses",       "uses"),
    "casts":           ("casts",      "casts"),
    "attrs":           ("attrs",      "attrs"),
    "accesses_of":     ("accesses_of", "accesses_of"),
    "accesses_on":     ("uses",       "accesses_on"),
    "all_refs":        ("text",       "all_refs"),
}


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # suppress per-request access log

    def _auth(self) -> bool:
        return self.headers.get("X-TYPESENSE-API-KEY") == API_KEY

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
        global _sync_thread, _watcher_paused

        path   = self.path.split("?")[0].rstrip("/")
        method = self.command

        # ── GET /health  (no auth required) ──────────────────────────────────
        if method == "GET" and path == "/health":
            self._send_json(200, {"ok": True})
            return

        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        # ── GET /status ───────────────────────────────────────────────────────
        if method == "GET" and path == "/status":
            result: dict = {}
            _watcher_running = bool(_watcher_thread and _watcher_thread.is_alive())
            _queue_depth     = _index_queue.depth
            if _watcher_paused:
                _watcher_state = "paused"
            elif not _watcher_running:
                _watcher_state = "stopped"
            elif _queue_depth > 0:
                _watcher_state = "processing"
            else:
                _watcher_state = "watching"
            result["watcher"] = {
                "running":     _watcher_running,
                "paused":      _watcher_paused,
                "state":       _watcher_state,
                "queue_depth": _queue_depth,
            }
            result["queue"] = _index_queue.stats()
            result["syncer"] = {
                "running": bool(_sync_thread and _sync_thread.is_alive()),
                "pending": len(_sync_pending),
            }
            if _PROGRESS_FILE.exists():
                try:
                    result["syncer"]["progress"] = json.loads(_PROGRESS_FILE.read_text())
                except Exception:
                    pass

            # Per-root collection status: live doc count + cached schema check
            collections: dict = {}
            for root_name in ROOTS:
                coll = collection_for_root(root_name)
                schema = _schema_status.get(root_name, {})
                ndocs: int | None = None
                if _ts_client is not None:
                    try:
                        info = _ts_client.collections[coll].retrieve()
                        ndocs = info.get("num_documents")
                    except Exception:
                        pass
                # Use the live Typesense retrieve() as the authoritative existence check;
                # the cached _schema_status may be stale (e.g. collection just created).
                col_live_exists = ndocs is not None
                collections[root_name] = {
                    "collection":        coll,
                    "num_documents":     ndocs,
                    "collection_exists": col_live_exists,
                    "schema_ok":         col_live_exists and schema.get("ok", False),
                    "schema_warnings":   schema.get("warnings", []) if col_live_exists else [],
                }
            result["collections"] = collections
            result["typesense_ok"] = _ts_healthy
            result["typesense_loading"] = _ts_initializing

            self._send_json(200, result)
            return

        # ── POST /check-ready ─────────────────────────────────────────────────
        if method == "POST" and path == "/check-ready":
            if _ts_initializing:
                self._send_json(200, {"ready": False, "loading": True,
                                      "error": "Typesense is still loading"})
                return
            body = self._read_body()
            root_arg  = body.get("root", "")
            root_name = root_arg if root_arg else ("default" if "default" in ROOTS else next(iter(ROOTS)))
            try:
                collection, src_root = get_root(root_name)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            result = check_ready(src_root=src_root, collection=collection,
                                 extensions=extensions_for_root(root_name))
            self._send_json(200, result)
            return

        # ── POST /verify/start  (alias for /index/start without resethard) ────
        if method == "POST" and path == "/verify/start":
            path = "/index/start"  # fall through; resethard defaults to False

        # ── POST /verify/stop ─────────────────────────────────────────────────
        if method == "POST" and path == "/verify/stop":
            if not (_sync_thread and _sync_thread.is_alive()):
                self._send_json(404, {"error": "no sync job is running"})
                return
            if _sync_stop:
                _sync_stop.set()
            self._send_json(200, {"stopped": True})
            return

        # ── POST /watcher/pause ───────────────────────────────────────────────
        if method == "POST" and path == "/watcher/pause":
            _watcher_paused = True
            with _watcher_lock:
                if _watcher_thread and _watcher_thread.is_alive():
                    _watcher_stop.set()
            print("[api] Watcher paused (Windows watcher active).", flush=True)
            self._send_json(200, {"paused": True})
            return

        # ── POST /watcher/resume ──────────────────────────────────────────────
        if method == "POST" and path == "/watcher/resume":
            _watcher_paused = False
            _start_watcher()
            print("[api] Watcher resumed.", flush=True)
            self._send_json(200, {"resumed": True})
            return

        # ── POST /file-events ─────────────────────────────────────────────────
        if method == "POST" and path == "/file-events":
            body   = self._read_body()
            events = body.get("events", [])
            result = _enqueue_file_events(events)
            self._send_json(200, result)
            return

        # ── POST /index/start ─────────────────────────────────────────────────
        if method == "POST" and path == "/index/start":
            if _ts_initializing:
                self._send_json(503, {"error": "Typesense is still loading, please wait", "loading": True})
                return
            body = self._read_body()
            root_arg  = body.get("root", "")
            root_name = root_arg if root_arg else ("default" if "default" in ROOTS else next(iter(ROOTS)))
            try:
                collection, src_root = get_root(root_name)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            resethard = bool(body.get("resethard", False))
            host_root = HOST_ROOTS.get(root_name, "")

            with _sync_lock:
                job = {"root_name": root_name, "src_root": src_root, "collection": collection,
                       "resethard": resethard, "host_root": host_root,
                       "extensions": extensions_for_root(root_name)}
                _sync_pending.append(job)
                queued_pos = len(_sync_pending)
                if not (_sync_thread and _sync_thread.is_alive()):
                    _sync_thread = threading.Thread(
                        target=_drain_sync_queue,
                        name="syncer",
                        daemon=True,
                    )
                    _sync_thread.start()
                    queued_pos = 0

            self._send_json(200, {
                "started":    queued_pos == 0,
                "queued":     queued_pos > 0,
                "position":   queued_pos,
                "collection": collection,
                "src_root":   src_root,
            })
            return

        # ── POST /query-codebase ──────────────────────────────────────────────
        if method == "POST" and path == "/query-codebase":
            if _ts_initializing:
                self._send_json(503, {"error": "Typesense is still loading, please wait", "loading": True})
                return
            body    = self._read_body()
            mode         = body.get("mode", "")
            pattern      = body.get("pattern", "")
            sub          = body.get("sub", "") or None
            ext          = body.get("ext", "") or None
            root         = body.get("root", "")
            limit        = int(body.get("limit", 50))
            include_body = bool(body.get("include_body", False))
            symbol_kind  = str(body.get("symbol_kind", "") or "")
            uses_kind    = str(body.get("uses_kind", "") or "")

            if mode not in _EXT_TO_TS_AND_AST:
                self._send_json(400, {"error": f"unknown mode: {mode!r}"})
                return

            ts_mode_flag, ast_mode = _EXT_TO_TS_AND_AST[mode]

            if not ROOTS:
                self._send_json(400, {"error": "No roots configured. Add a root with: ts root --add NAME /path/to/src"})
                return
            root_name = root if root else ("default" if "default" in ROOTS else next(iter(ROOTS)))
            try:
                collection, src_root = get_root(root_name)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            # Windows path prefix stored in Typesense relative_path (e.g. "C:/repos/src").
            # Must be stripped before prepending the server-local src_root.
            host_root_prefix = HOST_ROOTS.get(root_name, "").replace("\\", "/").rstrip("/")

            # Import search lazily from scripts/search.py (not a package, use importlib)
            import importlib.util as _ilu
            _search_spec = _ilu.spec_from_file_location(
                "scripts.search",
                os.path.join(_base, "scripts", "search.py"),
            )
            _search_mod = _ilu.module_from_spec(_search_spec)
            _search_spec.loader.exec_module(_search_mod)

            ts_kwargs = dict(
                query        = pattern,
                ext          = ext,
                sub          = sub,
                limit        = 250,  # always fetch Typesense max; `limit` controls AST depth
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

            import io as _io
            _ts_stdout_buf = _io.StringIO()
            try:
                import sys as _sys
                _old_stdout = _sys.stdout
                _sys.stdout = _ts_stdout_buf
                try:
                    ts_result, _ = _search_mod.search(**ts_kwargs)
                finally:
                    _sys.stdout = _old_stdout
            except SystemExit:
                detail = _ts_stdout_buf.getvalue().strip()
                self._send_json(503, {"error": "Typesense search failed", "detail": detail})
                return

            found     = ts_result.get("found", 0)
            hits      = ts_result.get("hits", [])
            facets    = ts_result.get("facet_counts", [])

            # Split: run AST on the first `limit` files; the rest get ast_expanded=False.
            ast_hits      = hits[:limit]
            pending_hits  = hits[limit:]

            # Resolve absolute paths for AST-eligible files
            file_list: list[str] = []
            hit_by_path: dict[str, dict] = {}
            native_src_root = os.path.realpath(to_native_path(src_root))
            for hit in ast_hits:
                rel = hit["document"].get("relative_path", "").replace("\\", "/")
                # Strip the Windows host_root prefix (e.g. "C:/repos/src/Foo.cs" →
                # "Foo.cs") so prepending the server-local src_root produces a valid path.
                # Without this, Docker produces "/source/default/C:/repos/src/Foo.cs".
                if host_root_prefix and rel.lower().startswith(host_root_prefix.lower() + "/"):
                    rel = rel[len(host_root_prefix) + 1:]
                abs_path = os.path.realpath(to_native_path(
                    src_root.rstrip("/\\") + "/" + rel
                ))
                # Ensure the resolved path is actually under src_root (prevents traversal).
                if not Path(abs_path).is_relative_to(native_src_root):
                    continue
                if os.path.isfile(abs_path):
                    file_list.append(abs_path)
                    hit_by_path[abs_path] = hit

            # Run AST query on eligible files
            ast_results = _run_query(ast_mode, pattern, file_list, include_body=include_body, symbol_kind=symbol_kind, uses_kind=uses_kind)

            # Build response: AST-expanded files (only those with matches)
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
                        "subsystem":     doc.get("subsystem", ""),
                        "filename":      doc.get("filename", ""),
                    },
                    "matches":      ast_item["matches"],
                    "ast_expanded": True,
                })

            # Append files where AST was not run (Typesense-only matches)
            for hit in pending_hits:
                doc = hit.get("document", {})
                response_hits.append({
                    "document": {
                        "id":            doc.get("id", ""),
                        "relative_path": doc.get("relative_path", ""),
                        "subsystem":     doc.get("subsystem", ""),
                        "filename":      doc.get("filename", ""),
                    },
                    "matches":      [],
                    "ast_expanded": False,
                })

            self._send_json(200, {
                # overflow=True means Typesense found more files than it can return (>250)
                "overflow":     found > len(hits),
                "found":        found,
                "hits":         response_hits,
                "facet_counts": facets,
            })
            return

        # ── POST /query ───────────────────────────────────────────────────────
        if method == "POST" and path == "/query":
            body         = self._read_body()
            mode         = body.get("mode", "")
            pattern      = body.get("pattern", "")
            files        = body.get("files", [])
            uses_kind_q  = str(body.get("uses_kind", "") or "")
            include_body = bool(body.get("include_body", False))
            symbol_kind  = str(body.get("symbol_kind", "") or "")
            if not mode:
                self._send_json(400, {"error": "mode required"})
                return
            if not isinstance(files, list) or not files:
                self._send_json(400, {"error": "files must be a non-empty list"})
                return
            try:
                safe_files = _resolve_query_paths(files)
                results = _run_query(mode, pattern, safe_files,
                                     include_body=include_body,
                                     symbol_kind=symbol_kind,
                                     uses_kind=uses_kind_q)
                self._send_json(200, {"results": results})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            return

        self._send_json(404, {"error": f"not found: {method} {path}"})

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        import traceback
        try:
            self._handle()
        except Exception:
            tb = traceback.format_exc()
            _CRASH_LOG = _RUN_DIR / "api_crash.log"
            try:
                with open(_CRASH_LOG, "a") as _f:
                    import datetime
                    _f.write(f"\n[{datetime.datetime.now().isoformat()}] {self.command} {self.path}\n{tb}\n")
            except Exception:
                pass
            print(f"[api] CRASH in handler: {tb}", flush=True)
            try:
                self._send_json(500, {"error": "internal server error", "detail": tb.splitlines()[-1]})
            except Exception:
                pass


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Async Typesense init (runs in background after HTTP server is up) ──────────

def _ts_init_loop(stop_event: threading.Event) -> None:
    """Poll until Typesense is healthy, then finish all init work.

    Runs as a daemon thread so the management API HTTP server can start
    (and answer /health) before Typesense is ready.  Sets _ts_initializing=False
    once the client, queue, schemas, watcher, and heartbeat are all up.
    """
    global _ts_client, _schema_status, _ts_initializing, _sync_thread

    print("[api] Waiting for Typesense to become ready…", flush=True)
    while not stop_event.is_set():
        if _ts_health():
            break
        stop_event.wait(2)

    if stop_event.is_set():
        return

    print("[api] Typesense ready — finishing initialization.", flush=True)

    _ts_client = get_client()
    _index_queue.start(_ts_client)
    _schema_status = verify_all_schemas(_ts_client)

    # Auto-sync all roots on every startup so the index is repaired after downtime
    with _sync_lock:
        for root_name in ROOTS:
            try:
                collection, src_root = get_root(root_name)
            except ValueError:
                continue
            _sync_pending.append({
                "root_name":  root_name,
                "src_root":   src_root,
                "collection": collection,
                "resethard":  False,
                "host_root":  HOST_ROOTS.get(root_name, ""),
                "extensions": extensions_for_root(root_name),
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
    print("[api] Initialization complete.", flush=True)


# ── main ───────────────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = API_PORT) -> None:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _API_PID.write_text(str(os.getpid()))

    print(f"[api] === STARTED pid={os.getpid()} at {time.strftime('%Y-%m-%dT%H:%M:%S')} ===", flush=True)

    # Graceful shutdown on SIGTERM
    _shutdown_event = threading.Event()
    def _on_sigterm(sig, frame):
        print("[api] SIGTERM received — shutting down…", flush=True)
        _shutdown_event.set()
    signal.signal(signal.SIGTERM, _on_sigterm)

    # Start HTTP server immediately — before Typesense is ready
    server = _ThreadedHTTPServer((host, port), _Handler)
    srv_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    srv_thread.start()
    print(f"[api] Listening on http://{host}:{port}", flush=True)

    # Kick off Typesense init in the background so callers don't have to wait
    _init_stop = threading.Event()
    _init_thread = threading.Thread(
        target=_ts_init_loop, args=(_init_stop,), name="ts-init", daemon=True
    )
    _init_thread.start()

    # Wait for shutdown signal
    _shutdown_event.wait()

    # Stop background init if still running
    _init_stop.set()

    # Stop all threads
    # server.shutdown() blocks until serve_forever() returns — run it with a timeout
    # so a slow/hung HTTP handler can't hold up the entire shutdown sequence.
    _srv_stop = threading.Thread(target=server.shutdown, daemon=True)
    _srv_stop.start()
    _srv_stop.join(timeout=5)
    if _srv_stop.is_alive():
        print("[api] HTTP server shutdown timed out — continuing anyway", flush=True)
    _hb_stop.set()
    _watcher_stop.set()
    if _sync_thread and _sync_thread.is_alive():
        if _sync_stop:
            _sync_stop.set()
        _sync_thread.join(timeout=5)
    if _ts_client is not None:
        _index_queue.stop(timeout=5)

    if _API_PID.exists():
        _API_PID.unlink()
    print("[api] Stopped.", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Indexserver management API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=API_PORT)
    args = ap.parse_args()
    run(host=args.host, port=args.port)
