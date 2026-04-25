"""
Typesense heartbeat watchdog.

Runs as a background process in WSL. Every CHECK_INTERVAL seconds it hits
the /health endpoint. After FAIL_THRESHOLD consecutive failures it restarts
the server. Also revives the file watcher if it dies while the server is healthy.

Usage (normally started by `ts start` or `ts heartbeat`):
    python heartbeat.py
"""

from __future__ import annotations

import os
import sys
import time
import datetime
import json
import subprocess
import urllib.request
from pathlib import Path

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.config import API_KEY, PORT, HOST, COLLECTION

_HOME          = Path.home()
_RUN_DIR       = _HOME / ".local" / "typesense"
_RUN_DIR.mkdir(parents=True, exist_ok=True)

_THIS_DIR      = Path(__file__).parent
_VENV_PY       = str(_HOME / ".local" / "indexserver-venv" / "bin" / "python3")
_SERVER_PY     = str(_THIS_DIR / "start_server.py")
_ENTRYPOINT    = str(_THIS_DIR.parent / "scripts" / "entrypoint.sh")
_WATCHER_PY    = str(_THIS_DIR / "watcher.py")
_SERVER_PID    = str(_RUN_DIR / "typesense.pid")
_WATCHER_PID   = str(_RUN_DIR / "watcher.pid")
_HEARTBEAT_PID = str(_RUN_DIR / "heartbeat.pid")
_INDEXER_PID   = str(_RUN_DIR / "indexer.pid")
_INDEXER_LOG   = str(_RUN_DIR / "indexer.log")
HEARTBEAT_LOG  = str(_RUN_DIR / "heartbeat.log")

CHECK_INTERVAL = 30
FAIL_THRESHOLD = 3
HEALTH_TIMEOUT = 5


# ── logging ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── probes ─────────────────────────────────────────────────────────────────────

def _health_ok() -> bool:
    url = f"http://{HOST}:{PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
            body = json.loads(r.read())
            return bool(body.get("ok", False))
    except Exception:
        return False


def _pid_alive(pid_file: str) -> bool:
    if not os.path.exists(pid_file):
        return False
    with open(pid_file) as _f:
        pid_str = _f.read().strip()
    if not pid_str:
        return False
    try:
        os.kill(int(pid_str), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


# ── index status ───────────────────────────────────────────────────────────────

def _index_status() -> str:
    url = f"http://{HOST}:{PORT}/collections/{COLLECTION}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            stats = json.loads(r.read())
        ndocs = stats.get("num_documents", 0)
        status = f"{ndocs:,} docs"
    except Exception:
        return "no index"

    if _pid_alive(_INDEXER_PID):
        progress = ""
        if os.path.exists(_INDEXER_LOG):
            try:
                with open(_INDEXER_LOG, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                last = lines[-1].rstrip() if lines else ""
                if len(last) > 80:
                    last = last[:77] + "..."
                if last:
                    progress = f" — {last}"
            except OSError:
                pass
        return f"{status} [indexing{progress}]"

    return status


# ── recovery ───────────────────────────────────────────────────────────────────

def _restart_server() -> None:
    _log("Stopping Typesense server...")
    subprocess.run([_VENV_PY, _SERVER_PY, "--stop"], capture_output=True)
    time.sleep(2)
    _log("Starting Typesense server...")
    env = os.environ.copy()
    env.update({
        "TYPESENSE_DATA":      str(_RUN_DIR),
        "CONFIG_FILE":         str(_THIS_DIR.parent / "config.json"),
        "APP_ROOT":            str(_THIS_DIR.parent),
        "PYTHON3":             _VENV_PY,
        "PYTHONPATH":          str(_THIS_DIR.parent),
        "CODESEARCH_API_HOST": "127.0.0.1",
    })
    result = subprocess.run(["bash", _ENTRYPOINT, "--background", "--disown"], env=env,
                            capture_output=True, text=True)
    if result.returncode == 0:
        _log("Server restarted OK.")
    else:
        _log(f"Server restart FAILED (rc={result.returncode}): {result.stderr[:300]}")


def _restart_watcher() -> None:
    _log("Restarting file watcher...")
    p = subprocess.Popen(
        [_VENV_PY, _WATCHER_PY],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(_WATCHER_PID, "w") as _f:
        _f.write(str(p.pid))
    _log(f"Watcher started (PID {p.pid})")


# ── main loop ──────────────────────────────────────────────────────────────────

def run() -> None:
    with open(_HEARTBEAT_PID, "w") as _f:
        _f.write(str(os.getpid()))
    _log(
        f"Heartbeat started  PID={os.getpid()}  "
        f"interval={CHECK_INTERVAL}s  threshold={FAIL_THRESHOLD}"
    )

    if not _pid_alive(_WATCHER_PID) and _health_ok():
        _log("Watcher not running - starting it...")
        _restart_watcher()

    failures = 0
    while True:
        time.sleep(CHECK_INTERVAL)

        if _health_ok():
            if failures > 0:
                _log(f"Server recovered after {failures} failure(s).")
            failures = 0
            _log(f"OK  index={_index_status()}")
        else:
            failures += 1
            _log(f"Health check FAILED ({failures}/{FAIL_THRESHOLD})  index=?")
            if failures >= FAIL_THRESHOLD:
                _restart_server()
                failures = 0

        if not _pid_alive(_WATCHER_PID) and _health_ok():
            _log("Watcher is dead - reviving...")
            _restart_watcher()


if __name__ == "__main__":
    run()
