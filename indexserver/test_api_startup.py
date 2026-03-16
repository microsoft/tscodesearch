"""
End-to-end test suite for api.py (indexserver management API).

Launches api.py if not already running, runs all endpoint tests,
then stops it if we started it.  If it's already running, tests
run against the live instance and it is left running afterwards.

Timeouts are progress-based: a test fails only if no measurable
progress has been seen for PROGRESS_TIMEOUT seconds, not on a fixed
wall-clock deadline.

Usage:
    ~/.local/indexserver-venv/bin/python3 test_api_startup.py
    ~/.local/indexserver-venv/bin/python3 test_api_startup.py --no-verify  # skip slow tests
"""

from __future__ import annotations

import argparse
import json
import select
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── locate files ──────────────────────────────────────────────────────────────

_THIS_DIR  = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

_API_PY  = _THIS_DIR / "api.py"
_HOME    = Path.home()
_VENV_PY = _HOME / ".local" / "indexserver-venv" / "bin" / "python3"
PYTHON   = str(_VENV_PY) if _VENV_PY.exists() else sys.executable

STARTUP_TIMEOUT  = 10   # max seconds to wait for initial /health
PROGRESS_TIMEOUT = 30   # seconds without any new activity before giving up
POLL_INTERVAL    = 2    # how often to sample /verify/status while waiting
LOG_INTERVAL     = 5    # how often to print a progress line while waiting

# ── test state ────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0
_skipped = 0


def _pass(label: str, detail: str = "") -> None:
    global _passed
    _passed += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [PASS] {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    global _failed
    _failed += 1
    suffix = f"\n         {detail}" if detail else ""
    print(f"  [FAIL] {label}{suffix}")


def _skip(label: str, reason: str = "") -> None:
    global _skipped
    _skipped += 1
    suffix = f"  ({reason})" if reason else ""
    print(f"  [SKIP] {label}{suffix}")


def _info(msg: str) -> None:
    print(f"         {msg}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _request(method: str, port: int, path: str, api_key: str,
             body: dict | None = None, timeout: int = 10) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"X-TYPESENSE-API-KEY": api_key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def _get(port, path, api_key, **kw):
    return _request("GET", port, path, api_key, **kw)

def _post(port, path, api_key, body=None, **kw):
    return _request("POST", port, path, api_key, body=body, **kw)


def _is_alive(port, api_key) -> bool:
    code, body = _get(port, "/health", api_key, timeout=3)
    return code == 200 and bool(body and body.get("ok"))


# ── progress-based wait helpers ───────────────────────────────────────────────

def _wait_blocking_with_liveness(port, api_key, request_fn,
                                  label: str) -> tuple[int, dict | None, float]:
    """
    Run a blocking HTTP call in a thread while pinging /health in the
    foreground.  Gives up if the server stops responding to /health for
    PROGRESS_TIMEOUT seconds.

    Returns (status_code, body, elapsed_s).
    """
    result: list = [None, None]
    done = threading.Event()

    def _work():
        result[0], result[1] = request_fn()
        done.set()

    t = threading.Thread(target=_work, daemon=True)
    t.start()

    last_alive = time.time()
    last_log   = time.time()
    t0         = time.time()

    while not done.wait(timeout=POLL_INTERVAL):
        alive = _is_alive(port, api_key)
        if alive:
            last_alive = time.time()
        else:
            gap = time.time() - last_alive
            _info(f"[{time.time()-t0:.0f}s] /health not responding ({gap:.0f}s since last ok)")
            if gap > PROGRESS_TIMEOUT:
                _info(f"  giving up — server stuck (no /health for {gap:.0f}s)")
                return 0, {"error": f"server stuck: no /health for {gap:.0f}s"}, time.time() - t0

        if time.time() - last_log >= LOG_INTERVAL:
            status_str = "server alive" if alive else f"server silent {time.time()-last_alive:.0f}s"
            _info(f"[{time.time()-t0:.0f}s] {label}: still running ({status_str})")
            last_log = time.time()

    return result[0] or 0, result[1] or {"error": "no result"}, time.time() - t0


def _fmt_verify_body(body: dict | None) -> str:
    """Format a /verify/status body into a compact progress line."""
    if not body:
        return "(no data)"
    parts = []
    if "running" in body:
        parts.append("running" if body["running"] else "stopped")
    if body.get("phase"):
        parts.append(f"phase={body['phase']}")
    if body.get("fs_files"):
        parts.append(f"fs={body['fs_files']:,}")
    if body.get("index_docs"):
        parts.append(f"index={body['index_docs']:,}")
    if body.get("missing") or body.get("stale") or body.get("orphaned"):
        parts.append(f"Δ={body.get('missing',0)}miss/"
                     f"{body.get('stale',0)}stale/{body.get('orphaned',0)}orphan")
    if body.get("updated"):
        parts.append(f"updated={body['updated']}")
    if body.get("status") and body["status"] not in ("running",):
        parts.append(f"status={body['status']}")
    return "  ".join(parts) if parts else str(body)


def _poll_with_progress(port, api_key, status_path: str,
                         done_fn,      # (body) -> bool: True when finished
                         progress_fn,  # (prev_body, curr_body) -> bool: True if made progress
                         label: str) -> tuple[bool, dict | None, float]:
    """
    Poll a status endpoint until done_fn returns True.
    Gives up if progress_fn returns False for PROGRESS_TIMEOUT seconds.

    Returns (success, final_body, elapsed_s).
    """
    t0 = time.time()
    last_progress = time.time()
    prev_body: dict | None = None
    last_log = time.time()

    while True:
        code, body = _get(port, status_path, api_key, timeout=5)
        if code not in (200, 404):
            _info(f"[{time.time()-t0:.0f}s] {label}: unexpected {code} — {body}")

        if body and done_fn(body):
            return True, body, time.time() - t0

        made_progress = prev_body is None or (body and progress_fn(prev_body, body))
        if made_progress and body:
            last_progress = time.time()
            prev_body = body
        else:
            stalled = time.time() - last_progress
            if stalled > PROGRESS_TIMEOUT:
                _info(f"[{time.time()-t0:.0f}s] {label}: no progress for {stalled:.0f}s — giving up")
                _info(f"  last status: {_fmt_verify_body(body)}")
                return False, body, time.time() - t0

        if time.time() - last_log >= LOG_INTERVAL:
            stalled = time.time() - last_progress
            stall_str = f"  stalled {stalled:.0f}s" if stalled > 5 else ""
            _info(f"[{time.time()-t0:.0f}s] {label}: {_fmt_verify_body(body)}{stall_str}")
            last_log = time.time()

        time.sleep(POLL_INTERVAL)


# ── config detection ──────────────────────────────────────────────────────────

def _get_config() -> tuple[int, str]:
    r = subprocess.run(
        [PYTHON, "-c",
         "from indexserver.config import API_PORT, API_KEY; "
         "print(API_PORT); print(API_KEY)"],
        capture_output=True, text=True, cwd=str(_REPO_ROOT),
    )
    if r.returncode == 0:
        lines = r.stdout.strip().splitlines()
        return int(lines[0]), lines[1]
    return 8109, "codesearch-local"


# ── startup / teardown ────────────────────────────────────────────────────────

def _already_running(port: int, api_key: str) -> bool:
    return _is_alive(port, api_key)


def _launch(port: int, api_key: str) -> subprocess.Popen | None:
    proc = subprocess.Popen(
        [PYTHON, str(_API_PY)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(_REPO_ROOT),
    )

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _already_running(port, api_key):
            return proc
        time.sleep(0.4)

    # Failed — collect output and diagnose
    output = []
    if proc.stdout:
        while True:
            r, _, _ = select.select([proc.stdout], [], [], 0.1)
            if not r:
                break
            line = proc.stdout.readline()
            if not line:
                break
            output.append(line.rstrip())

    print(f"\n  Process exited with code {proc.poll()}")
    if output:
        print("  Output:")
        for line in output:
            _info(line)

    # Syntax check
    r = subprocess.run(
        [PYTHON, "-c",
         f"import sys; sys.path.insert(0,'{_REPO_ROOT}'); "
         f"import py_compile; py_compile.compile('{_API_PY}', doraise=True)"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("  Syntax error:")
        for line in r.stderr.strip().splitlines():
            _info(line)

    return None


# ── test cases ────────────────────────────────────────────────────────────────

def test_prerequisites() -> bool:
    ok = True

    r = subprocess.run([PYTHON, "--version"], capture_output=True, text=True)
    if r.returncode == 0:
        _pass("Python available", r.stdout.strip())
    else:
        _fail("Python not found", PYTHON)
        ok = False

    if _API_PY.exists():
        _pass("api.py exists")
    else:
        _fail("api.py not found", str(_API_PY))
        ok = False

    for module, attr in [
        ("indexserver.config",   "API_PORT"),
        ("indexserver.verifier", "check_ready"),
        ("indexserver.watcher",  "run_watcher"),
    ]:
        r = subprocess.run(
            [PYTHON, "-c", f"from {module} import {attr}"],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        if r.returncode == 0:
            _pass(f"import {module}.{attr}")
        else:
            last_line = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
            _fail(f"import {module}.{attr}", last_line)
            ok = False

    return ok


def test_health(port, api_key):
    code, body = _get(port, "/health", api_key)
    if code == 200 and body and body.get("ok"):
        _pass("GET /health", '{"ok": true}')
    else:
        _fail("GET /health", f"code={code} body={body}")


def test_auth_rejection(port, api_key):
    code, _ = _get(port, "/health", "wrong-key")
    if code == 401:
        _pass("Auth rejection (wrong key → 401)")
    else:
        _fail("Auth rejection (wrong key)", f"expected 401, got {code}")

    code2, _ = _get(port, "/health", "")
    if code2 == 401:
        _pass("Auth rejection (no key → 401)")
    else:
        _fail("Auth rejection (no key)", f"expected 401, got {code2}")


def test_status(port, api_key):
    code, body = _get(port, "/status", api_key)
    if code != 200:
        _fail("GET /status", f"code={code} body={body}")
        return
    _pass("GET /status returns 200")

    watcher = body.get("watcher", {})
    if "running" in watcher:
        _pass("GET /status — watcher.running present",
              "running" if watcher["running"] else "stopped")
    else:
        _fail("GET /status — watcher.running missing", str(body))

    if "verifier" in body and "running" in body["verifier"]:
        _pass("GET /status — verifier.running present")
    else:
        _fail("GET /status — verifier.running missing", str(body))

    queue = body.get("queue", {})
    expected_queue_keys = {"depth", "enqueued", "deduped", "upserted", "deleted", "skipped", "errors"}
    missing = expected_queue_keys - set(queue.keys())
    if not missing:
        _pass("GET /status — queue stats present",
              f"depth={queue.get('depth', '?')}  upserted={queue.get('upserted', '?')}")
    else:
        _fail("GET /status — queue stats missing keys", str(missing))

    indexer = body.get("indexer", {})
    if "running" in indexer:
        _pass("GET /status — indexer.running present",
              "running" if indexer["running"] else "idle")
    else:
        _fail("GET /status — indexer.running missing", str(body))


def test_unknown_route(port, api_key):
    code, _ = _get(port, "/does-not-exist", api_key)
    if code == 404:
        _pass("Unknown route → 404")
    else:
        _fail("Unknown route", f"expected 404, got {code}")


def test_check_ready_bad_root(port, api_key):
    code, body = _post(port, "/check-ready", api_key, body={"root": "nonexistent_root"})
    if code == 400:
        _pass("POST /check-ready bad root → 400")
    else:
        _fail("POST /check-ready bad root", f"expected 400, got {code} {body}")


def test_check_ready(port, api_key):
    _info(f"sending request — will fail if server goes silent for >{PROGRESS_TIMEOUT}s...")

    code, body, elapsed = _wait_blocking_with_liveness(
        port, api_key,
        request_fn=lambda: _post(port, "/check-ready", api_key,
                                 body={"root": "default"}, timeout=3600),
        label="POST /check-ready",
    )

    if code == 0:
        _fail("POST /check-ready", f"server went silent after {elapsed:.0f}s: {body}")
        return
    if code != 200:
        _fail("POST /check-ready", f"code={code}  elapsed={elapsed:.1f}s  body={body}")
        return
    _pass("POST /check-ready returns 200", f"{elapsed:.1f}s")

    expected_keys = {"ready", "poll_ok", "index_ok", "fs_files",
                     "indexed", "missing", "stale", "orphaned", "duration_s"}
    missing = expected_keys - set(body.keys())
    if not missing:
        _pass("POST /check-ready — response shape correct")
    else:
        _fail("POST /check-ready — missing keys", str(missing))
        return

    if body.get("poll_ok"):
        _pass("POST /check-ready — poll_ok=true",
              f"fs_files={body['fs_files']:,}  indexed={body['indexed']:,}  "
              f"server_duration={body['duration_s']:.1f}s  total={elapsed:.1f}s")
    else:
        _fail("POST /check-ready — poll_ok=false", body.get("error", str(body)))

    status = "up to date" if body.get("index_ok") else (
        f"NOT up to date — {body['missing']} missing, "
        f"{body['stale']} stale, {body['orphaned']} orphaned"
    )
    _pass("POST /check-ready — index_ok", status)


def test_verify_lifecycle(port, api_key):
    # -- status before any run
    code, body = _get(port, "/verify/status", api_key)
    if code == 404:
        _pass("GET /verify/status (no prior run) → 404")
    elif code == 200:
        _pass("GET /verify/status (prior run exists) → 200")
    else:
        _fail("GET /verify/status (before start)", f"code={code} body={body}")

    # -- start
    code, body = _post(port, "/verify/start", api_key,
                       body={"root": "default", "delete_orphans": False})
    if code == 200 and body and body.get("started"):
        _pass("POST /verify/start → 200 started=true",
              f"collection={body.get('collection')}")
    elif code == 409:
        _skip("POST /verify/start", "already running — skipping lifecycle test")
        return
    else:
        _fail("POST /verify/start", f"code={code} body={body}")
        return

    # -- double-start must 409
    code2, _ = _post(port, "/verify/start", api_key,
                     body={"root": "default", "delete_orphans": False})
    if code2 == 409:
        _pass("POST /verify/start (duplicate) → 409 conflict")
    else:
        _fail("POST /verify/start (duplicate)", f"expected 409, got {code2}")

    # -- wait until running=true (progress: running flips to True)
    def _running_true(body):   return bool(body and body.get("running"))
    def _any_change(prev, cur): return prev != cur

    ok, body, elapsed = _poll_with_progress(
        port, api_key, "/verify/status",
        done_fn=_running_true,
        progress_fn=_any_change,
        label="wait for running=true",
    )
    if ok:
        _pass("GET /verify/status while running → running=true",
              f"phase={body.get('phase','?')}  fs_files={body.get('fs_files','?')}  ({elapsed:.0f}s)")
    else:
        # Might have finished instantly
        c, b = _get(port, "/verify/status", api_key)
        if c == 200 and b and b.get("status") in ("done", "cancelled"):
            _pass("GET /verify/status — completed instantly", b.get("status"))
            return
        _fail("GET /verify/status while running",
              f"never saw running=true after {elapsed:.0f}s  last={body}")
        return

    # -- stop
    code, body = _post(port, "/verify/stop", api_key)
    if code == 200:
        _pass("POST /verify/stop → 200")
    else:
        _fail("POST /verify/stop", f"code={code} body={body}")
        return

    # -- wait until running=false (progress: status or running field changes)
    def _running_false(body):  return bool(body and not body.get("running"))
    def _status_changed(prev, cur):
        return (prev.get("running") != cur.get("running") or
                prev.get("status")  != cur.get("status") or
                prev.get("phase")   != cur.get("phase"))

    ok, body, elapsed = _poll_with_progress(
        port, api_key, "/verify/status",
        done_fn=_running_false,
        progress_fn=_status_changed,
        label="wait for running=false",
    )
    if ok:
        _pass("GET /verify/status after stop → running=false",
              f"status={body.get('status','?')}  ({elapsed:.0f}s)")
    else:
        _fail("GET /verify/status after stop — still running",
              f"after {elapsed:.0f}s  last={body}")
        return

    # -- stop when not running → 404
    code, body = _post(port, "/verify/stop", api_key)
    if code == 404:
        _pass("POST /verify/stop (not running) → 404")
    else:
        _fail("POST /verify/stop (not running)", f"expected 404, got {code} body={body}")


def test_query_codebase(port, api_key):
    """Tests for POST /query-codebase endpoint."""

    # Unknown mode → 400
    code, body = _post(port, "/query-codebase", api_key,
                       body={"mode": "nonexistent_mode", "pattern": "Foo"})
    if code == 400:
        _pass("POST /query-codebase unknown mode → 400",
              body.get("error", "") if body else "")
    else:
        _fail("POST /query-codebase unknown mode", f"expected 400, got {code} body={body}")

    # Valid mode, empty pattern — should return a valid response
    code, body = _post(port, "/query-codebase", api_key,
                       body={"mode": "uses", "pattern": "", "ext": "cs"})
    if code == 200 and body is not None:
        _pass("POST /query-codebase valid mode → 200")
        # Check shape
        has_found    = "found"    in body
        has_overflow = "overflow" in body
        has_hits     = "hits"     in body
        has_facets   = "facet_counts" in body
        if has_found and has_overflow and has_hits and has_facets:
            _pass("POST /query-codebase response shape correct",
                  f"found={body['found']} overflow={body['overflow']} hits={len(body['hits'])}")
        else:
            _fail("POST /query-codebase response shape",
                  f"missing keys — got {list(body.keys())}")
    else:
        _fail("POST /query-codebase valid mode", f"code={code} body={body}")

    # Overflow case: use limit=1 — if any files match, should overflow
    code, body = _post(port, "/query-codebase", api_key,
                       body={"mode": "uses", "pattern": "string", "limit": 1})
    if code == 200 and body is not None:
        if body.get("overflow"):
            _pass("POST /query-codebase overflow case works",
                  f"found={body.get('found')} overflow=true hits={len(body.get('hits', []))}")
            if body.get("hits") == []:
                _pass("POST /query-codebase overflow: hits is empty list")
            else:
                _fail("POST /query-codebase overflow: hits should be empty", str(body.get("hits")))
        elif body.get("found", 0) == 0:
            _skip("POST /query-codebase overflow case", "no 'string' matches — index may be empty")
        else:
            _fail("POST /query-codebase overflow", f"expected overflow=true, got body={body}")
    else:
        _fail("POST /query-codebase overflow", f"code={code} body={body}")

    # Hits with matches have document field
    code, body = _post(port, "/query-codebase", api_key,
                       body={"mode": "uses", "pattern": "string", "limit": 50})
    if code == 200 and body is not None and not body.get("overflow"):
        hits = body.get("hits", [])
        if hits:
            h = hits[0]
            has_doc     = "document"  in h
            has_matches = "matches"   in h
            if has_doc and has_matches:
                doc = h["document"]
                doc_ok = "relative_path" in doc
                _pass("POST /query-codebase hits have document+matches",
                      f"rel_path={doc.get('relative_path','?')} matches={len(h['matches'])}")
                if doc_ok:
                    _pass("POST /query-codebase document has relative_path")
                else:
                    _fail("POST /query-codebase document missing relative_path", str(doc))
            else:
                _fail("POST /query-codebase hit missing document or matches", str(h))
        else:
            _skip("POST /query-codebase hit document check",
                  "no hits returned (empty index or no matches for 'string')")
    elif body and body.get("overflow"):
        _skip("POST /query-codebase hit document check", "overflowed at limit=50")
    else:
        _fail("POST /query-codebase document check", f"code={code} body={body}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the slow /check-ready and verify lifecycle tests")
    args = ap.parse_args()

    print("=" * 60)
    print("  api.py end-to-end test suite")
    print(f"  progress timeout: {PROGRESS_TIMEOUT}s")
    print("=" * 60)

    print("\n[1] Prerequisites")
    if not test_prerequisites():
        print("\nFATAL: prerequisites failed — cannot continue.")
        sys.exit(1)

    api_port, api_key = _get_config()
    print(f"       api_port={api_port}  api_key={api_key!r}")

    print("\n[2] Server startup")
    we_started = False
    proc = None

    if _already_running(api_port, api_key):
        _pass("api.py already running — using live instance")
    else:
        _info("Launching api.py...")
        proc = _launch(api_port, api_key)
        if proc is None:
            _fail("api.py failed to start")
            print("\nFATAL: cannot run endpoint tests without a running server.")
            sys.exit(1)
        we_started = True
        _pass("api.py started", f"PID {proc.pid}")

    print("\n[3] Endpoint tests")
    test_health(api_port, api_key)
    test_auth_rejection(api_port, api_key)
    test_status(api_port, api_key)
    test_unknown_route(api_port, api_key)
    test_check_ready_bad_root(api_port, api_key)
    test_query_codebase(api_port, api_key)

    if args.no_verify:
        _skip("POST /check-ready", "--no-verify")
        _skip("Verify lifecycle", "--no-verify")
    else:
        print("\n[4] Slow tests (FS walk + verify lifecycle)")
        test_check_ready(api_port, api_key)
        test_verify_lifecycle(api_port, api_key)

    if we_started and proc:
        print("\n[5] Teardown")
        proc.terminate()
        try:
            proc.wait(timeout=5)
            _pass("api.py stopped cleanly")
        except subprocess.TimeoutExpired:
            proc.kill()
            _pass("api.py killed (did not stop in 5s)")

    total = _passed + _failed + _skipped
    print(f"\n{'=' * 60}")
    print(f"  Results: {_passed} passed, {_failed} failed, {_skipped} skipped  ({total} total)")
    print(f"{'=' * 60}")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
