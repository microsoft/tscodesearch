"""
Typesense service manager for code search.

Commands:
    status    - Show server health, document count, watcher/verifier state
    start     - Start Typesense server + indexserver (watcher + heartbeat built-in)
    stop      - Stop indexserver and Typesense server
    restart   - stop then start
    index     - Run indexer in background (add --resethard to wipe data and reindex)
    verify    - Scan the file system and repair stale/missing index entries
    log       - Tail the server or indexer log

Usage:
    python service.py <command> [options]
    ts.cmd <command> [options]
"""

from __future__ import annotations

import os
import sys
import signal
import subprocess
import argparse
import time
import urllib.request
import urllib.error
import json
from pathlib import Path

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.config import (
    API_KEY, PORT, API_PORT, HOST, COLLECTION, ROOTS, collection_for_root,
)

# ── paths ──────────────────────────────────────────────────────────────────────
_HOME         = Path.home()
# Support Docker: TYPESENSE_DATA env var overrides default location
_RUN_DIR      = Path(os.environ.get("TYPESENSE_DATA", _HOME / ".local" / "typesense"))
_RUN_DIR.mkdir(parents=True, exist_ok=True)

_THIS_DIR     = Path(__file__).parent                           # indexserver/
_REPO_ROOT    = str(_THIS_DIR.parent)                           # repo root

# Support Docker: use system Python if venv doesn't exist
_VENV_PY_PATH = _HOME / ".local" / "indexserver-venv" / "bin" / "python3"
_VENV_PY      = str(_VENV_PY_PATH) if _VENV_PY_PATH.exists() else sys.executable
_SERVER_PY    = str(_THIS_DIR / "start_server.py")
_ENTRYPOINT   = str(_THIS_DIR.parent / "scripts" / "entrypoint.sh")

_INDEXER_LOG  = str(_RUN_DIR / "indexer.log")
_SERVER_PID   = str(_RUN_DIR / "typesense.pid")
_SERVER_LOG   = str(_RUN_DIR / "typesense.log")
_SERVER_ERR   = str(_RUN_DIR / "typesense-error.log")
_API_PID      = str(_RUN_DIR / "api.pid")
_INDEXER_PID  = str(_RUN_DIR / "indexer.pid")
_WATCHER_STATS = str(_RUN_DIR / "watcher_stats.json")


# ── helpers ────────────────────────────────────────────────────────────────────

def _pid_alive(pid_file: str) -> tuple[bool, str]:
    if not os.path.exists(pid_file):
        return False, ""
    pid_str = open(pid_file).read().strip()
    if not pid_str:
        return False, ""
    try:
        os.kill(int(pid_str), 0)
        return True, pid_str
    except (OSError, ProcessLookupError, ValueError):
        return False, pid_str


def _typesense_health() -> dict:
    url = f"http://{HOST}:{PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            body = json.loads(r.read())
            return {"ok": body.get("ok", False), "status": "healthy"}
    except Exception as e:
        return {"ok": False, "status": str(e)}


def _collection_stats(collection: str) -> dict | None:
    url = f"http://{HOST}:{PORT}/collections/{collection}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _api_status() -> dict | None:
    """Call GET /status on the indexserver management API."""
    url = f"http://{HOST}:{API_PORT}/status"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _api_post(path: str, body: dict) -> tuple[int, dict]:
    """POST to the indexserver management API. Returns (status_code, response_dict)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://{HOST}:{API_PORT}{path}",
        data=data,
        headers={"X-TYPESENSE-API-KEY": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def _kill_pid(pid_file: str, label: str, wait_secs: float = 5.0) -> None:
    alive, pid_str = _pid_alive(pid_file)
    if alive:
        pid = int(pid_str)
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait for graceful exit
            deadline = time.time() + wait_secs
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.2)
                except (OSError, ProcessLookupError):
                    break
            else:
                try:
                    print(f"  {label} (PID {pid_str}) did not stop in {wait_secs:.0f}s — sending SIGKILL")
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.3)
                except (OSError, ProcessLookupError):
                    pass
            print(f"  Stopped {label} (PID {pid_str})")
        except OSError:
            print(f"  {label}: kill failed (PID {pid_str})")
    else:
        print(f"  {label}: not running")
    if os.path.exists(pid_file):
        os.remove(pid_file)


# ── commands ───────────────────────────────────────────────────────────────────

def _entrypoint_env() -> dict:
    """Build the environment for entrypoint.sh --background calls."""
    env = os.environ.copy()
    env.update({
        "TYPESENSE_DATA":      str(_RUN_DIR),
        "CONFIG_FILE":         str(Path(_REPO_ROOT) / "config.json"),
        "APP_ROOT":            _REPO_ROOT,
        "PYTHON3":             _VENV_PY,
        "PYTHONPATH":          _REPO_ROOT,
        "CODESEARCH_API_HOST": "127.0.0.1",
    })
    return env


def cmd_status(args) -> None:
    print("-- Typesense Service Status ------------------------------------------")

    server_alive, server_pid = _pid_alive(_SERVER_PID)
    health = _typesense_health()
    if health["ok"]:
        print(f"  Server  : [OK]  running  (pid={server_pid}, port={PORT})")
    elif server_alive:
        print(f"  Server  : [!!] process alive (pid={server_pid}) but health failed: {health['status']}")
    else:
        print(f"  Server  : [--] not running")

    api_alive, api_pid = _pid_alive(_API_PID)
    api_info = _api_status() if api_alive else None

    missing_collections = []
    api_collections = (api_info or {}).get("collections", {})
    for root_name, _src in ROOTS.items():
        coll_name = collection_for_root(root_name)
        coll_info = api_collections.get(root_name)
        if coll_info:
            # api is up — use its validated collection status (schema checked server-side)
            ndocs        = coll_info.get("num_documents") or 0
            warnings     = coll_info.get("schema_warnings") or []
            col_exists   = coll_info.get("collection_exists", coll_info.get("num_documents") is not None)
            indexer_running = (api_info or {}).get("indexer", {}).get("running", False)
            if not col_exists:
                if indexer_running:
                    print(f"  [{root_name}] Index : [>>] indexing in progress ({ndocs:,} docs so far)")
                    # Don't add to missing_collections — searches will work once done
                else:
                    print(f"  [{root_name}] Index : [--] not yet indexed — run: ts index")
                    missing_collections.append(root_name)
            elif warnings:
                print(f"  [{root_name}] Index : [!!] schema outdated ({ndocs:,} docs)")
                for w in warnings:
                    print(f"             {w}")
                print(f"             Fix: ts index --root {root_name} --resethard")
                missing_collections.append(root_name)
            else:
                print(f"  [{root_name}] Index : [OK]  {ndocs:,} docs  ({coll_name})")
        elif health["ok"]:
            # api is down but Typesense is up — doc count only, schema unverified
            stats = _collection_stats(coll_name)
            if stats:
                ndocs = stats.get("num_documents", 0)
                print(f"  [{root_name}] Index : [?]  {ndocs:,} docs  (schema unverified — indexserver not running)")
            else:
                print(f"  [{root_name}] Index : [!!] '{coll_name}' not found — searches will fail")
                missing_collections.append(root_name)
        else:
            print(f"  [{root_name}] Index : (server unavailable)")

    if api_alive:
        print(f"  API     : [OK]  running  (PID {api_pid}, port={API_PORT})")
    else:
        print(f"  API     : [--] not running")

    if api_info:
        watcher = api_info.get("watcher", {})
        paused  = watcher.get("paused", False)
        if watcher.get("running"):
            print(f"  Watcher : [OK]  running (thread)")
        elif paused:
            print(f"  Watcher : [OK]  paused (Windows watcher active)")
        else:
            print(f"  Watcher : [--] not running")

        queue = api_info.get("queue", {})
        if queue:
            depth    = queue.get("depth", 0)
            upserted = queue.get("upserted", 0)
            deleted  = queue.get("deleted", 0)
            deduped  = queue.get("deduped", 0)
            errors   = queue.get("errors", 0)
            depth_note = f"  [{depth} waiting]" if depth else ""
            err_note   = f"  errors={errors}" if errors else ""
            print(f"  Queue   : {upserted} upserted, {deleted} deleted, {deduped} deduped{depth_note}{err_note}")

        indexer_info = api_info.get("indexer", {})
        if indexer_info.get("running"):
            prog = indexer_info.get("progress", {})
            disc = prog.get("discovered", 0)
            qdep = prog.get("queue_depth", queue.get("depth", 0) if queue else 0)
            print(f"  Indexer : [>>] running  (discovered={disc:,}  queue={qdep:,})")
        elif indexer_info.get("progress"):
            prog   = indexer_info["progress"]
            status = prog.get("status", "idle")
            disc   = prog.get("discovered", 0)
            print(f"  Indexer : {status}  (last run: {disc:,} files discovered)")

        verifier = api_info.get("verifier", {})
        if verifier.get("running"):
            prog  = verifier.get("progress", {})
            total = prog.get("total_to_update", 0)
            done  = prog.get("updated", 0)
            if total:
                pct = f"{done * 100 // total}%"
                print(f"  Verifier: [>>] running  ({pct}  {done:,}/{total:,} updated)")
            else:
                phase = prog.get("phase", "starting")
                print(f"  Verifier: [>>] running  ({phase})")
        else:
            prog = verifier.get("progress", {})
            if prog:
                vstatus  = prog.get("status", "idle")
                missing  = prog.get("missing", 0)
                stale    = prog.get("stale", 0)
                fs_files = prog.get("fs_files", 0)
                indexed  = prog.get("index_docs", 0)
                detail_parts = []
                if fs_files:
                    detail_parts.append(f"{indexed:,}/{fs_files:,} indexed")
                if missing:
                    detail_parts.append(f"{missing:,} missing")
                if stale:
                    detail_parts.append(f"{stale:,} stale")
                detail = f"  ({', '.join(detail_parts)})" if detail_parts else ""
                print(f"  Verifier: {vstatus}{detail}")

    if missing_collections:
        print(f"")
        print(f"  !! SEARCHES WILL FAIL — index unavailable for: {', '.join(missing_collections)}")
    print("----------------------------------------------------------------------")


def _to_native_path(path: str) -> str:
    """Convert a Windows-format path (X:/...) to the native path for this process."""
    import re as _re
    path = path.replace("\\", "/")
    if sys.platform == "linux":
        m = _re.match(r"^([a-zA-Z]):(.*)", path)
        if m:
            path = f"/mnt/{m.group(1).lower()}{m.group(2)}"
    return path


def _check_typesense_locks() -> bool:
    """Check Typesense RocksDB lock files before starting.

    If lock files exist and are actively held by another process → print an
    error and return False (caller should abort).

    If lock files exist but are not held → warn and return True; Typesense
    will open them itself on startup (do NOT delete them — that can force a
    more expensive recovery path).

    Uses fcntl.flock(LOCK_NB) to probe whether the lock is actually held,
    which is more reliable than PID-file checks alone.
    """
    import fcntl

    data_dir = _RUN_DIR / "data"
    lock_paths = [data_dir / "db" / "LOCK", data_dir / "meta" / "LOCK"]
    present = [p for p in lock_paths if p.exists()]
    if not present:
        return True  # nothing to worry about

    held = []
    stale = []
    for p in present:
        try:
            with open(p, "r+b") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            stale.append(p)   # we got the lock — previous holder is gone
        except OSError:
            held.append(p)    # EWOULDBLOCK — another process holds it

    if held:
        server_alive, server_pid = _pid_alive(_SERVER_PID)
        pid_info = f" by PID {server_pid}" if server_alive and server_pid else ""
        print(f"ERROR: Typesense lock file(s) are held{pid_info}:")
        for p in held:
            print(f"  {p}")
        if server_alive and server_pid:
            print(f"  Stop the server first:  ts stop")
            print(f"  Or force-kill:           kill -9 {server_pid}")
        else:
            print("  An unknown process holds the lock — find it with:")
            print(f"  fuser {held[0]}")
        return False

    if stale:
        print("  Note: stale Typesense lock file(s) found (not held — Typesense will clear them on open):")
        for p in stale:
            print(f"    {p}")
    return True


def cmd_start(args) -> None:
    if not API_KEY or not API_KEY.strip():
        print("ERROR: api_key is missing or blank in config.json.")
        print("       Delete config.json and re-run setup.cmd to regenerate it.")
        sys.exit(1)

    for root_name, raw_path in ROOTS.items():
        native = _to_native_path(raw_path)
        if not os.path.isdir(native):
            print(f"ERROR: Source directory for root '{root_name}' does not exist: {native}")
            print(f"       Check 'roots.{root_name}' in config.json, then run: ts restart")
            sys.exit(1)

    if not _check_typesense_locks():
        sys.exit(1)

    result = subprocess.run(["bash", _ENTRYPOINT, "--background", "--disown"], env=_entrypoint_env())
    if result.returncode != 0:
        print("ERROR: startup failed — check logs with: ts log")
        sys.exit(1)

    cmd_status(args)


def cmd_stop(args) -> None:
    print("Stopping services...")

    # Stop standalone indexer subprocess if running (from ts index)
    indexer_alive, indexer_pid = _pid_alive(_INDEXER_PID)
    if indexer_alive:
        try:
            os.kill(int(indexer_pid), signal.SIGTERM)
            print(f"  Stopped indexer (PID {indexer_pid})")
        except OSError:
            pass
    if os.path.exists(_INDEXER_PID):
        os.remove(_INDEXER_PID)

    # Stop indexserver (watcher + heartbeat + verifier thread all stop via SIGTERM)
    _kill_pid(_API_PID, "indexserver")

    print("  Stopping Typesense server...")
    try:
        subprocess.run([_VENV_PY, _SERVER_PY, "--stop"], timeout=20)
    except subprocess.TimeoutExpired:
        print("  WARNING: Typesense stop timed out — force-killing any remaining process")
        subprocess.run(["pkill", "-9", "-f", "typesense-server"], capture_output=True)
    if os.path.exists(_SERVER_PID):
        os.remove(_SERVER_PID)


def cmd_restart(args) -> None:
    cmd_stop(args)
    cmd_start(args)


def cmd_index(args) -> None:
    import shutil

    indexer_alive, indexer_pid = _pid_alive(_INDEXER_PID)
    if indexer_alive:
        print(f"Indexer already running (PID {indexer_pid}). Stop it first with: ts stop")
        sys.exit(1)

    if args.resethard:
        print("Hard reset: stopping all services...")

        # Stop any standalone indexer subprocess
        indexer_alive2, indexer_pid2 = _pid_alive(_INDEXER_PID)
        if indexer_alive2:
            try:
                os.kill(int(indexer_pid2), signal.SIGTERM)
                print(f"  Stopped indexer (PID {indexer_pid2})")
            except OSError:
                pass
        if os.path.exists(_INDEXER_PID):
            os.remove(_INDEXER_PID)

        # Stop indexserver (watcher + heartbeat + in-process indexer thread)
        _kill_pid(_API_PID, "indexserver")

        # Stop Typesense
        print("  Stopping Typesense server...")
        try:
            subprocess.run([_VENV_PY, _SERVER_PY, "--stop"], timeout=20)
        except subprocess.TimeoutExpired:
            print("  WARNING: Typesense stop timed out — force-killing")
            subprocess.run(["pkill", "-9", "-f", "typesense-server"], capture_output=True)
        if os.path.exists(_SERVER_PID):
            os.remove(_SERVER_PID)

        # Full wipe
        if _RUN_DIR.exists():
            shutil.rmtree(str(_RUN_DIR))
            print(f"  Wiped {_RUN_DIR}")
        _RUN_DIR.mkdir(parents=True, exist_ok=True)

        # Reinstall Typesense binary
        result = subprocess.run([_VENV_PY, _SERVER_PY, "--install"])
        if result.returncode != 0:
            print("ERROR: failed to reinstall Typesense binary")
            sys.exit(1)

        # Start Typesense, run initial index, start indexserver
        print("Starting services and reindexing...")
        result = subprocess.run(["bash", _ENTRYPOINT, "--background", "--disown"], env=_entrypoint_env())
        if result.returncode != 0:
            print("ERROR: startup failed after resethard — check logs with: ts log")
            sys.exit(1)
        return

    elif not _typesense_health()["ok"]:
        print("ERROR: Typesense server is not running. Start it first with: ts start")
        sys.exit(1)

    api_alive, _ = _pid_alive(_API_PID)
    if not api_alive:
        print("ERROR: indexserver is not running. Start it with: ts start")
        sys.exit(1)

    root_name = getattr(args, "root", None) or (
        "default" if "default" in ROOTS else next(iter(ROOTS))
    )
    if root_name not in ROOTS:
        print(f"ERROR: Unknown root '{root_name}'. Available: {sorted(ROOTS)}")
        sys.exit(1)

    coll_name = collection_for_root(root_name)
    src_path  = ROOTS[root_name]

    print(f"Starting indexer for root '{root_name}' {'(--resethard) ' if args.resethard else ''}...")
    print(f"  Collection : {coll_name}")
    print(f"  Source     : {src_path}")

    code, result = _api_post("/index/start", {
        "root":      root_name,
        "resethard": args.resethard,
    })
    if code == 409:
        print("Indexer is already running. Monitor with: ts status")
        return
    if code != 200:
        print(f"ERROR: indexserver returned {code}: {result.get('error', result)}")
        sys.exit(1)

    print(f"  Indexer running (in-process, queue depth shown in: ts status)")
    print(f"  Monitor with: ts status")


def cmd_verify(args) -> None:
    api_alive, _ = _pid_alive(_API_PID)
    if not api_alive:
        print("ERROR: indexserver is not running. Start it with: ts start")
        sys.exit(1)

    root_name = getattr(args, "root", None) or (
        "default" if "default" in ROOTS else next(iter(ROOTS))
    )
    if root_name not in ROOTS:
        print(f"ERROR: Unknown root '{root_name}'. Available: {sorted(ROOTS)}")
        sys.exit(1)

    delete_orphans = not getattr(args, "no_delete_orphans", False)

    code, result = _api_post("/verify/start", {
        "root": root_name,
        "delete_orphans": delete_orphans,
    })

    if code == 409:
        print("A verification scan is already running. Monitor with: ts status")
        return
    if code != 200:
        print(f"ERROR: indexserver returned {code}: {result.get('error', result)}")
        sys.exit(1)

    print(f"Verification scan started.")
    print(f"  Root       : '{root_name}' → {ROOTS[root_name]}")
    print(f"  Collection : {result.get('collection', '?')}")
    print(f"  Monitor with: ts status")


def _tail_log(path: str, n: int, label: str) -> None:
    if not os.path.exists(path):
        print(f"No {label} log found.")
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in lines[-n:]:
        print(line, end="")


def cmd_log(args) -> None:
    n = args.lines or 40
    if args.indexer:
        _tail_log(_INDEXER_LOG, n, "indexer")
    elif args.error:
        _tail_log(_SERVER_ERR, n, "server error")
    else:
        _tail_log(_SERVER_LOG, n, "server")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", metavar="command")

    sub.add_parser("status",  help="Show service status")
    sub.add_parser("start",   help="Start server + indexserver (watcher + heartbeat)")
    sub.add_parser("stop",    help="Stop indexserver + server")
    sub.add_parser("restart", help="Restart server + indexserver")

    p_idx = sub.add_parser("index", help="Run indexer in background")
    p_idx.add_argument("--resethard", action="store_true",
                       help="Stop server, wipe data directory, restart, and reindex from scratch")
    p_idx.add_argument("--root", default=None,
                       help="Named root to index (default: first configured root)")

    p_ver = sub.add_parser("verify", help="Scan the file system and repair stale/missing index entries")
    p_ver.add_argument("--root", default=None,
                       help="Named root to verify (default: first configured root)")
    p_ver.add_argument("--no-delete-orphans", dest="no_delete_orphans", action="store_true",
                       help="Keep index entries for files that no longer exist on disk")

    p_log = sub.add_parser("log", help="Show server or indexer log")
    p_log.add_argument("--indexer",   action="store_true", help="Show indexer log")
    p_log.add_argument("--error",     action="store_true", help="Show server error log (stderr)")
    p_log.add_argument("--lines", "-n", type=int, default=40, help="Lines to show (default 40)")

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        sys.exit(0)

    dispatch = {
        "status":    cmd_status,
        "start":     cmd_start,
        "stop":      cmd_stop,
        "restart":   cmd_restart,
        "index":     cmd_index,
        "verify":    cmd_verify,
        "log":       cmd_log,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
