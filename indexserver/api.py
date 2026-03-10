"""
Indexserver — single Python process for the entire indexserver stack.

Combines in one process:
  - HTTP management API   port PORT+1, authenticated with the same API key
  - File watcher          thread: PollingObserver on all configured roots
  - Heartbeat watchdog    thread: checks Typesense health every 30 s
  - Verifier              on-demand thread: run_verify() started via POST /verify/start

Started by: ts start
Stopped by: ts stop (SIGTERM → graceful shutdown of all threads)

Endpoints (all require X-TYPESENSE-API-KEY header):
  GET  /health              → {"ok": true}
  GET  /status              → watcher stats, verifier state
  POST /check-ready         → body {"root": "default"} → check_ready() result
  POST /verify/start        → body {"root": "default", "delete_orphans": true}
  GET  /verify/status       → verifier_progress.json + {"running": bool}
  POST /verify/stop         → cancel running verify
  POST /watcher/pause       → stop PollingObserver thread; heartbeat won't auto-revive it
  POST /watcher/resume      → restart PollingObserver thread (clears pause flag)
  POST /file-events         → body {"events": [{"path": "Q:/src/foo.cs", "action": "upsert"|"delete"}]}
                              Receives real-time change notifications from the Windows watcher
                              (win-watcher/watcher.mjs). Paths are Windows drive-letter style;
                              converted to native WSL/Linux paths before indexing.
  POST /query               → body {"mode": "calls", "pattern": "MethodName", "files": ["/abs/path.cs"]}
                              Run a tree-sitter C# AST query against the given files.
                              Returns {"results": [{"file": path, "matches": [{"line": N, "text": "..."}]}]}
                              Modes: calls, implements, uses, field_type, param_type, casts, ident,
                                     member_accesses, attrs, find, params, classes, methods, fields, usings
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

import typesense

from indexserver.config import (
    API_KEY, API_PORT, PORT, HOST, ROOTS, get_root,
    INCLUDE_EXTENSIONS, EXCLUDE_DIRS, MAX_FILE_BYTES,
    to_native_path, collection_for_root, TYPESENSE_CLIENT_CONFIG,
)
from indexserver.index_queue import IndexQueue
from indexserver.indexer import walk_and_enqueue, walk_source_files, ensure_collection, get_client
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

CHECK_INTERVAL = 30    # heartbeat poll interval (seconds)
FAIL_THRESHOLD = 3     # consecutive failures before restarting Typesense

# ── thread state ───────────────────────────────────────────────────────────────
_watcher_stop  = threading.Event()
_watcher_thread: threading.Thread | None = None
_watcher_lock  = threading.Lock()

_verify_stop   = threading.Event()
_verify_thread: threading.Thread | None = None
_verify_lock   = threading.Lock()

_index_thread: threading.Thread | None = None
_index_lock   = threading.Lock()
_index_progress: dict = {}   # updated by the in-process indexer thread

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
        (to_native_path(win_root).rstrip("/"), collection_for_root(name))
        for name, win_root in ROOTS.items()
    ]

    n_new = n_dedup = 0
    for ev in events:
        raw_path = ev.get("path", "").replace("\\", "/")
        action   = ev.get("action", "upsert")
        ext      = os.path.splitext(raw_path)[1].lower()

        if ext not in INCLUDE_EXTENSIONS:
            continue

        native_path = to_native_path(raw_path)

        coll = native_root = None
        for nr, c in root_map:
            if native_path.startswith(nr + "/"):
                native_root, coll = nr, c
                break
        if coll is None:
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

def _run_index_thread(src_root: str, collection: str, resethard: bool, stop_event: threading.Event) -> None:
    """Walk src_root and feed every file into _index_queue (runs as a thread in api.py).

    Retries up to 3 times (with a 15 s delay) if Typesense isn't fully ready
    yet after a hard reset.  On retry the collection is NOT dropped again —
    ensure_collection will create it if it doesn't exist.
    """
    global _index_progress
    _INDEXER_PID.write_text(str(os.getpid()))
    max_attempts = 3
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                _index_progress = {
                    "status":     "starting",
                    "collection": collection,
                    "src_root":   src_root,
                    "attempt":    attempt,
                }
                prefix = f"(attempt {attempt}/{max_attempts}) " if attempt > 1 else ""
                print(f"[indexer] {prefix}Starting {'(resethard) ' if resethard else ''}for {src_root} → {collection}", flush=True)
                n_new, n_dedup = walk_and_enqueue(
                    src_root, collection, _index_queue,
                    reset=resethard, stop_event=stop_event,
                )
                _index_progress = {
                    "status":     "queued" if not stop_event.is_set() else "stopped",
                    "collection": collection,
                    "discovered": n_new + n_dedup,
                    "deduped":    n_dedup,
                    "queue_depth": _index_queue.depth,
                }
                print(f"[indexer] Walk complete: {n_new} queued, {n_dedup} deduped, queue depth={_index_queue.depth}", flush=True)
                break  # success
            except Exception as e:
                if attempt < max_attempts and not stop_event.is_set():
                    print(f"[indexer] ERROR (attempt {attempt}/{max_attempts}): {e} — retrying in 15 s…", flush=True)
                    stop_event.wait(15)
                    resethard = False  # collection already dropped on first attempt
                else:
                    raise
    except Exception as e:
        _index_progress = {"status": "error", "error": str(e)}
        print(f"[indexer] ERROR: {e}", flush=True)
    finally:
        if _INDEXER_PID.exists():
            _INDEXER_PID.unlink()


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
    result = subprocess.run([_VENV_PY, _SERVER_PY], capture_output=True, text=True)
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
    failures = 0
    # Give Typesense a moment to be ready before first check
    if stop_event.wait(10):
        return

    while not stop_event.is_set():
        if _ts_health():
            if failures:
                print(f"[heartbeat] Server recovered after {failures} failure(s).", flush=True)
            failures = 0
        else:
            failures += 1
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
        import query as _q  # codesearch/query.py — on sys.path via _base above
        _query_module = _q
    return _query_module


def _run_query(mode: str, pattern: str, files: list) -> list:
    """Run a tree-sitter C# AST query against a list of absolute file paths.

    Returns a list of {"file": path, "matches": [{"line": N, "text": "..."}]}
    where line is 1-indexed.  Only files with at least one match are included.
    """
    _q = _get_query_module()

    dispatch = {
        "classes":         lambda s, t, l: _q.q_classes(s, t, l),
        "methods":         lambda s, t, l: _q.q_methods(s, t, l),
        "fields":          lambda s, t, l: _q.q_fields(s, t, l),
        "usings":          lambda s, t, l: _q.q_usings(s, t, l),
        "calls":           lambda s, t, l: _q.q_calls(s, t, l, pattern),
        "implements":      lambda s, t, l: _q.q_implements(s, t, l, pattern),
        "uses":            lambda s, t, l: _q.q_uses(s, t, l, pattern),
        "field_type":      lambda s, t, l: _q.q_field_type(s, t, l, pattern),
        "param_type":      lambda s, t, l: _q.q_param_type(s, t, l, pattern),
        "casts":           lambda s, t, l: _q.q_casts(s, t, l, pattern),
        "ident":           lambda s, t, l: _q.q_ident(s, t, l, pattern),
        "member_accesses": lambda s, t, l: _q.q_member_accesses(s, t, l, pattern),
        "attrs":           lambda s, t, l: _q.q_attrs(s, t, l, pattern or None),
        "find":            lambda s, t, l: _q.q_find(s, t, l, pattern),
        "params":          lambda s, t, l: _q.q_params(s, t, l, pattern),
    }

    fn = dispatch.get(mode)
    if fn is None:
        raise ValueError(f"unknown mode: {mode!r}")

    results = []
    for file_path in files:
        native = to_native_path(file_path)
        try:
            src_bytes = open(native, "rb").read()
        except OSError:
            continue
        try:
            tree = _q._parser.parse(src_bytes)
        except Exception:
            continue
        lines = src_bytes.decode("utf-8", errors="replace").splitlines()
        raw = fn(src_bytes, tree, lines)
        if raw:
            results.append({
                "file":    file_path,   # return original path so caller can match it back
                "matches": [{"line": ln, "text": text} for ln, text in raw],
            })
    return results


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
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                pass
        return {}

    def _handle(self) -> None:
        global _verify_thread, _verify_stop, _watcher_paused, _index_thread

        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        path   = self.path.split("?")[0].rstrip("/")
        method = self.command

        # ── GET /health ───────────────────────────────────────────────────────
        if method == "GET" and path == "/health":
            self._send_json(200, {"ok": True})
            return

        # ── GET /status ───────────────────────────────────────────────────────
        if method == "GET" and path == "/status":
            result: dict = {}
            result["watcher"] = {
                "running": bool(_watcher_thread and _watcher_thread.is_alive()),
                "paused":  _watcher_paused,
            }
            result["queue"] = _index_queue.stats()
            result["indexer"] = {
                "running":  bool(_index_thread and _index_thread.is_alive()),
                "progress": _index_progress,
            }
            result["verifier"] = {
                "running": bool(_verify_thread and _verify_thread.is_alive())
            }
            if _PROGRESS_FILE.exists():
                try:
                    result["verifier"]["progress"] = json.loads(_PROGRESS_FILE.read_text())
                except Exception:
                    pass
            self._send_json(200, result)
            return

        # ── POST /check-ready ─────────────────────────────────────────────────
        if method == "POST" and path == "/check-ready":
            body = self._read_body()
            try:
                collection, src_root = get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            result = check_ready(src_root=src_root, collection=collection)
            self._send_json(200, result)
            return

        # ── POST /verify/start ────────────────────────────────────────────────
        if method == "POST" and path == "/verify/start":
            body   = self._read_body()
            delete = body.get("delete_orphans", True)
            try:
                collection, src_root = get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return

            with _verify_lock:
                if _verify_thread and _verify_thread.is_alive():
                    self._send_json(409, {"error": "verify already running"})
                    return

                _verify_stop = threading.Event()

                def _run():
                    _INDEXER_PID.write_text(str(os.getpid()))
                    try:
                        run_verify(
                            src_root=src_root,
                            collection=collection,
                            delete_orphans=delete,
                            stop_event=_verify_stop,
                        )
                    finally:
                        if _INDEXER_PID.exists():
                            _INDEXER_PID.unlink()

                _verify_thread = threading.Thread(
                    target=_run, name="verifier", daemon=True
                )
                _verify_thread.start()

            self._send_json(200, {
                "started":    True,
                "collection": collection,
                "src_root":   src_root,
            })
            return

        # ── GET /verify/status ────────────────────────────────────────────────
        if method == "GET" and path == "/verify/status":
            if not _PROGRESS_FILE.exists():
                self._send_json(404, {"error": "no verify scan has been run"})
                return
            try:
                data = json.loads(_PROGRESS_FILE.read_text())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            data["running"] = bool(_verify_thread and _verify_thread.is_alive())
            self._send_json(200, data)
            return

        # ── POST /verify/stop ─────────────────────────────────────────────────
        if method == "POST" and path == "/verify/stop":
            if not (_verify_thread and _verify_thread.is_alive()):
                self._send_json(404, {"error": "no verify job is running"})
                return
            _verify_stop.set()
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
            body = self._read_body()
            try:
                collection, src_root = get_root(body.get("root", ""))
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            resethard = bool(body.get("resethard", False))

            with _index_lock:
                if _index_thread and _index_thread.is_alive():
                    self._send_json(409, {"error": "index already running"})
                    return
                _index_stop = threading.Event()
                _index_thread = threading.Thread(
                    target=_run_index_thread,
                    args=(src_root, collection, resethard, _index_stop),
                    name="indexer",
                    daemon=True,
                )
                _index_thread.start()

            self._send_json(200, {
                "started":    True,
                "collection": collection,
                "src_root":   src_root,
            })
            return

        # ── POST /query ───────────────────────────────────────────────────────
        if method == "POST" and path == "/query":
            body    = self._read_body()
            mode    = body.get("mode", "")
            pattern = body.get("pattern", "")
            files   = body.get("files", [])
            if not mode:
                self._send_json(400, {"error": "mode required"})
                return
            if not isinstance(files, list) or not files:
                self._send_json(400, {"error": "files must be a non-empty list"})
                return
            try:
                results = _run_query(mode, pattern, files)
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


# ── main ───────────────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = API_PORT) -> None:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _API_PID.write_text(str(os.getpid()))

    # Graceful shutdown on SIGTERM
    _shutdown_event = threading.Event()
    def _on_sigterm(sig, frame):
        print("[api] SIGTERM received — shutting down…", flush=True)
        _shutdown_event.set()
    signal.signal(signal.SIGTERM, _on_sigterm)

    # Start the index queue worker (needs a Typesense client — server must be up first)
    _index_queue.start(get_client())

    # Start watcher thread
    _start_watcher()

    # Start heartbeat thread
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(_hb_stop,), name="heartbeat", daemon=True
    )
    _hb_thread.start()

    # Start HTTP server in background thread
    server = _ThreadedHTTPServer((host, port), _Handler)
    srv_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    srv_thread.start()
    print(f"[api] Listening on http://{host}:{port}", flush=True)

    # Wait for shutdown signal
    _shutdown_event.wait()

    # Stop all threads
    server.shutdown()
    _hb_stop.set()
    _watcher_stop.set()
    if _verify_thread and _verify_thread.is_alive():
        _verify_stop.set()
        _verify_thread.join(timeout=5)
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
