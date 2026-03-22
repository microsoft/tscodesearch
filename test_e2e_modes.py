#!/usr/bin/env python3
"""
End-to-end smoke test for tscodesearch covering both Docker and WSL modes.

Usage:
    python test_e2e_modes.py <source-directory>

Example:
    python test_e2e_modes.py C:\\repos\\benchmarkstreamjsonrpc

What is tested:
  Docker mode
    - ts start / ts stop lifecycle
    - search_code: Typesense search returns results
    - search_code filename: relative_path is the full Windows path (C:/...)
    - query_single_file: /query with /source/<name>/... container path succeeds
    - query_single_file: /query with a bare Windows path C:/... returns no matches
      (C:/ is not accessible inside the container -- expected behaviour)

  WSL mode
    - ts start / ts stop lifecycle
    - search_code: same Typesense search returns results
    - search_code filename: relative_path is a bare relative path (no root prefix)
      because HOST_ROOTS is not written for WSL configs
    - query_single_file: /query with a WSL-native /mnt/<drive>/... path succeeds
    - query_single_file: /query with a /source/<name>/... container path returns
      no matches (that directory does not exist in WSL -- known gap)

WARNING: This script temporarily overwrites config.json and stops any running
codesearch service on port 8108.  The original config.json is restored on exit.
"""

import sys
import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
import subprocess
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
TS_MJS      = SCRIPT_DIR / "ts.mjs"

# ── Log file ───────────────────────────────────────────────────────────────────

_ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = SCRIPT_DIR / f"e2e_test_{_ts}.log"
_log_fh: Optional[object] = None


def _log_open() -> None:
    global _log_fh
    _log_fh = open(LOG_PATH, "w", encoding="utf-8", buffering=1)


def _log_close() -> None:
    if _log_fh:
        _log_fh.close()


def log(msg: str = "", flush: bool = True) -> None:
    """Print to console and append to log file."""
    print(msg, flush=flush)
    if _log_fh:
        _log_fh.write(msg + "\n")
        _log_fh.flush()


# ── Test accounting ────────────────────────────────────────────────────────────

_passed = 0
_failed = 0


def ok(msg: str) -> None:
    global _passed
    _passed += 1
    log(f"    PASS  {msg}")


def fail(msg: str, detail: str = "") -> None:
    global _failed
    _failed += 1
    suffix = f"\n          {detail}" if detail else ""
    log(f"    FAIL  {msg}{suffix}")


def section(title: str) -> None:
    log(f"\n{'-' * 62}")
    log(f"  {title}")
    log(f"{'-' * 62}")


# ── Config helpers ─────────────────────────────────────────────────────────────

_API_KEY = "smoke-test-key"


def write_config(src_dir: str, mode: str, port: int = 8108) -> None:
    """Write a minimal config.json for one test run."""
    cfg = {
        "api_key":          _API_KEY,
        "port":             port,
        "mode":             mode,
        "docker_container": "codesearch",
        "docker_image":     "codesearch-mcp",
        "roots": {
            "default": {"windows_path": src_dir.replace("\\", "/").rstrip("/")}
        },
    }
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ── Service management ─────────────────────────────────────────────────────────

def ts(*cmd: str, timeout: int = 180) -> int:
    """Run `node ts.mjs <cmd...>`, tee output to log file, return exit code."""
    log(f"  [cmd] node ts.mjs {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        ["node", str(TS_MJS), *cmd],
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            if _log_fh:
                _log_fh.write(line + "\n")
                _log_fh.flush()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log(f"  [cmd] TIMED OUT after {timeout}s")
        return 1
    return proc.returncode


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str, headers: Optional[dict] = None, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, headers: Optional[dict] = None, timeout: int = 15) -> dict:
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ts_health(port: int) -> bool:
    try:
        return _get(f"http://localhost:{port}/health", timeout=3).get("ok", False)
    except Exception:
        return False


def collection_doc_count(port: int, collection: str) -> int:
    try:
        data = _get(
            f"http://localhost:{port}/collections/{collection}",
            headers={"X-TYPESENSE-API-KEY": _API_KEY},
        )
        return int(data.get("num_documents", 0))
    except Exception:
        return 0


def ts_search(port: int, collection: str, query: str,
              limit: int = 20, host: str = "localhost") -> list:
    """Run a broad text search against Typesense. Returns the list of hits."""
    params = urllib.parse.urlencode({
        "q":        query,
        "query_by": "filename,class_names,method_names,content",
        "per_page": limit,
    })
    url = f"http://{host}:{port}/collections/{collection}/documents/search?{params}"
    data = _get(url, headers={"X-TYPESENSE-API-KEY": _API_KEY})
    return data.get("hits", [])


def api_query(api_port: int, mode: str, files: list, pattern: str = "",
              host: str = "localhost") -> dict:
    """POST to indexserver /query.  Returns the full response dict."""
    return _post(
        f"http://{host}:{api_port}/query",
        {"mode": mode, "pattern": pattern, "files": files},
        headers={"X-TYPESENSE-API-KEY": _API_KEY},
    )


def _wsl_search(ts_host: str, port: int, query: str) -> list:
    """search_code: basic search returns at least one hit (WSL-IP-aware)."""
    try:
        hits = ts_search(port, "codesearch_default", query, host=ts_host)
    except Exception as e:
        fail(f"search '{query}': request failed", str(e))
        return []
    if hits:
        ok(f"search '{query}': {len(hits)} hit(s)")
    else:
        fail(f"search '{query}': returned no results")
    return hits


def _wait_api_ready_host(host: str, api_port: int, timeout: int = 10) -> bool:
    """Block until the management API responds on /health (host-aware)."""
    log(f"    Waiting for management API on {host}:{api_port}...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(f"http://{host}:{api_port}/health",
                 headers={"X-TYPESENSE-API-KEY": _API_KEY}, timeout=3)
            log(f"    Management API ready", flush=True)
            return True
        except Exception:
            time.sleep(1)
    log(f"    Management API not ready after {timeout}s", flush=True)
    return False


# ── Readiness poll ─────────────────────────────────────────────────────────────

def ts_health_detail(port: int) -> tuple[bool, str]:
    """Like ts_health but returns (ok, error_detail)."""
    url = f"http://localhost:{port}/health"
    try:
        result = _get(url, timeout=3)
        ok = result.get("ok", False)
        return ok, "" if ok else f"response={result}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def wait_ready(port: int, collection: str = "codesearch_default",
               timeout: int = 10) -> bool:
    """Block until Typesense is healthy and the collection has documents."""
    log(f"    Waiting for service (port {port}, collection {collection!r})", flush=True)
    deadline = time.time() + timeout
    last_n = -1
    last_err = ""
    while time.time() < deadline:
        ok, err = ts_health_detail(port)
        if ok:
            n = collection_doc_count(port, collection)
            if n != last_n:
                log(f"      health OK, docs={n}", flush=True)
                last_n = n
            if n > 0:
                log(f"    Ready ({n:,} docs)", flush=True)
                return True
        else:
            if err != last_err:
                log(f"      health check failed: {err}", flush=True)
                last_err = err
        time.sleep(1)
    log(f"    TIMED OUT after {timeout}s", flush=True)
    return False


def get_wsl2_ip() -> Optional[str]:
    """Return the WSL2 VM IP address (accessible from Windows)."""
    try:
        r = subprocess.run(
            ["wsl.exe", "bash", "-lc", "hostname -I | awk '{print $1}'"],
            capture_output=True, text=True, timeout=8,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        ip = r.stdout.strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return ip
    except Exception:
        pass
    return None


def wait_ready(port: int, collection: str = "codesearch_default",
               timeout: int = 10, host: str = "localhost") -> bool:
    """Block until Typesense is healthy and the collection has documents."""
    log(f"    Waiting for service ({host}:{port}, collection {collection!r})", flush=True)
    deadline = time.time() + timeout
    last_n = -1
    last_err = ""
    while time.time() < deadline:
        url = f"http://{host}:{port}/health"
        try:
            result = _get(url, timeout=3)
            ok = result.get("ok", False)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if err != last_err:
                log(f"      health check failed: {err}", flush=True)
                last_err = err
            ok = False
        if ok:
            try:
                col_url = f"http://{host}:{port}/collections/{collection}"
                data = _get(col_url, headers={"X-TYPESENSE-API-KEY": _API_KEY}, timeout=3)
                n = int(data.get("num_documents", 0))
            except Exception:
                n = 0
            if n != last_n:
                log(f"      health OK, docs={n}", flush=True)
                last_n = n
            if n > 0:
                log(f"    Ready ({n:,} docs)", flush=True)
                return True
        time.sleep(1)
    log(f"    TIMED OUT after {timeout}s", flush=True)
    return False


def wait_api_ready(api_port: int, timeout: int = 10) -> bool:
    """Block until the management API (indexserver) responds on /health."""
    log(f"    Waiting for management API on port {api_port}...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(f"http://localhost:{api_port}/health",
                 headers={"X-TYPESENSE-API-KEY": _API_KEY}, timeout=3)
            log(f"    Management API ready", flush=True)
            return True
        except Exception:
            time.sleep(1)
    log(f"    Management API not ready after {timeout}s", flush=True)
    return False


# ── Path conversion helpers ────────────────────────────────────────────────────

def win_to_wsl(p: str) -> str:
    """C:/repos/foo  ->  /mnt/c/repos/foo"""
    p = p.replace("\\", "/")
    m = re.match(r"^([a-zA-Z]):(.*)", p)
    if m:
        return f"/mnt/{m.group(1).lower()}{m.group(2)}"
    return p


def to_container_path(win_path: str, src_dir: str,
                      root_name: str = "default") -> str:
    """
    Replicates mcp_server.ts toContainerPath():
      C:/repos/foo/src/File.cs  ->  /source/default/src/File.cs
    """
    p        = win_path.replace("\\", "/")
    root     = src_dir.replace("\\", "/").rstrip("/")
    p_low    = p.lower()
    root_low = root.lower()
    if p_low.startswith(root_low + "/") or p_low == root_low:
        rel = p[len(root):]          # includes leading "/"
        return f"/source/{root_name}{rel}"
    # Bare relative -- prepend root and retry
    if not re.match(r"^[A-Za-z]:", p) and not p.startswith("/source/"):
        return to_container_path(root + "/" + p.lstrip("/"), src_dir, root_name)
    return p                          # already a container path or unknown


# ── Individual test cases ──────────────────────────────────────────────────────

def check_search(port: int, query: str = "IBenchmarkService",
                 host: str = "localhost") -> list:
    """search_code: basic search returns at least one hit."""
    try:
        hits = ts_search(port, "codesearch_default", query, host=host)
    except Exception as e:
        fail(f"search '{query}': request failed", str(e))
        return []
    if hits:
        ok(f"search '{query}': {len(hits)} hit(s)")
    else:
        fail(f"search '{query}': returned no results")
    return hits


def check_filename_docker(hits: list, src_dir: str) -> Optional[str]:
    """
    In Docker mode the indexer stores HOST_ROOTS-prefixed paths, so
    relative_path must start with the Windows source root.
    Returns one path for downstream query tests.
    """
    root_low = src_dir.replace("\\", "/").rstrip("/").lower()
    good = [h["document"]["relative_path"] for h in hits
            if h["document"].get("relative_path", "").replace("\\", "/").lower()
               .startswith(root_low + "/")]
    sample = hits[0]["document"].get("relative_path", "") if hits else ""
    if good:
        ok(f"filename (Docker): full Windows path  e.g. {good[0]!r}")
        return good[0]
    fail("filename (Docker): relative_path does not start with source root",
         f"expected prefix {root_low!r}  got {sample!r}")
    return sample or None


def check_filename_wsl(hits: list, src_dir: str) -> Optional[str]:
    """
    In WSL mode HOST_ROOTS is not written, so relative_path is a bare
    relative path with no drive-letter or root prefix.
    Returns one path for downstream query tests.
    """
    root_low = src_dir.replace("\\", "/").rstrip("/").lower()
    paths    = [h["document"].get("relative_path", "") for h in hits]
    bare     = [p for p in paths if p and not re.match(r"^[A-Za-z]:", p)
                                         and not p.startswith("/")]
    full     = [p for p in paths if p.lower().startswith(root_low + "/")]

    if bare:
        ok(f"filename (WSL): bare relative path  e.g. {bare[0]!r}")
        return bare[0]
    if full:
        # host_roots was somehow set -- not the default but not wrong
        ok(f"filename (WSL): full Windows path (host_roots active)  e.g. {full[0]!r}")
        return full[0]
    fail("filename (WSL): unexpected relative_path format",
         f"sample={paths[:3]}")
    return paths[0] if paths else None


def check_qsf_docker(api_port: int, relative_path: str, src_dir: str) -> None:
    """
    query_single_file in Docker mode.

    1. /source/default/...  path  ->  should return method matches  (PASS expected)
    2. C:/...  Windows path        ->  should return no matches because
       the container has no C:/ drive  (expected behavior -- PASS expected)
    """
    container_path = to_container_path(relative_path, src_dir)
    log(f"    qsf Docker: container_path={container_path!r}")
    log(f"    qsf Docker: win_path={relative_path!r}")

    # Case 1: container path
    try:
        result  = api_query(api_port, "methods", [container_path])
        matches = (result.get("results") or [{}])[0].get("matches", [])
        if matches:
            ok(f"query_single_file Docker /source/ path: {len(matches)} method(s)"
               f"  ({container_path!r})")
        else:
            fail("query_single_file Docker /source/ path: no matches",
                 f"path={container_path!r}  full response={json.dumps(result)[:300]}")
    except Exception as e:
        fail("query_single_file Docker /source/ path: request failed", str(e))

    # Case 2: raw Windows path -- no C:/ inside container
    win_path = relative_path.replace("\\", "/")
    try:
        result  = api_query(api_port, "methods", [win_path])
        matches = (result.get("results") or [{}])[0].get("matches", [])
        if not matches:
            ok("query_single_file Docker C:/ path: no matches (expected -- C:/ not in container)")
        else:
            fail("query_single_file Docker C:/ path: unexpected matches",
                 "C:/... paths should be unresolvable inside the Docker container")
    except Exception as e:
        fail("query_single_file Docker C:/ path: request failed", str(e))


def check_qsf_wsl(api_host: str, api_port: int, relative_path: str, src_dir: str) -> None:
    """
    query_single_file in WSL mode.

    1. /mnt/<drive>/...  path  ->  should return method matches  (PASS expected)
    2. /source/default/... path ->  should return no matches because WSL has no
       /source/ directory (known gap in mcp_server.js path conversion for WSL)
    """
    src_norm = src_dir.replace("\\", "/").rstrip("/")
    wsl_root = win_to_wsl(src_norm)

    # Build WSL-native absolute path from whatever format relative_path is in
    if re.match(r"^[A-Za-z]:", relative_path):
        # Full Windows path already (host_roots active)
        wsl_path = win_to_wsl(relative_path)
    elif relative_path.startswith("/mnt/"):
        wsl_path = relative_path
    else:
        # Bare relative path -- prepend WSL root
        wsl_path = wsl_root + "/" + relative_path.replace("\\", "/").lstrip("/")

    log(f"    qsf WSL: wsl_path={wsl_path!r}")

    # Case 1: WSL-native /mnt/... path
    try:
        result  = api_query(api_port, "methods", [wsl_path], host=api_host)
        matches = (result.get("results") or [{}])[0].get("matches", [])
        if matches:
            ok(f"query_single_file WSL /mnt/ path: {len(matches)} method(s)"
               f"  ({wsl_path!r})")
        else:
            fail("query_single_file WSL /mnt/ path: no matches",
                 f"path={wsl_path!r}  full response={json.dumps(result)[:300]}")
    except Exception as e:
        fail("query_single_file WSL /mnt/ path: request failed", str(e))

    # Case 2: container /source/default/... path (what mcp_server.js currently sends)
    rel = (relative_path if not re.match(r"^[A-Za-z]:", relative_path)
           else relative_path[len(src_norm):])
    rel = rel.replace("\\", "/").lstrip("/")
    container_path = f"/source/default/{rel}"
    log(f"    qsf WSL: container_path={container_path!r}")
    try:
        result  = api_query(api_port, "methods", [container_path], host=api_host)
        matches = (result.get("results") or [{}])[0].get("matches", [])
        if not matches:
            ok("query_single_file WSL /source/ path: no matches"
               " (expected -- /source/ not mounted in WSL)")
        else:
            fail("query_single_file WSL /source/ path: unexpected matches",
                 "/source/... paths should not be resolvable in WSL")
    except Exception as e:
        fail("query_single_file WSL /source/ path: request failed", str(e))


# ── Mode runners ───────────────────────────────────────────────────────────────

def wipe_docker() -> None:
    """Hard-wipe: stop container, remove it, remove data volume."""
    log("  Hard wipe: stopping + removing Docker container...", flush=True)
    ts("stop")
    r = subprocess.run(["docker", "volume", "rm", "codesearch_data"],
                       capture_output=True, text=True)
    if _log_fh:
        _log_fh.write(f"  docker volume rm: rc={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}\n")
    log(f"  volume rm rc={r.returncode}", flush=True)


def run_docker(src_dir: str) -> None:
    port     = 8108
    api_port = port + 1

    section("Docker mode -- starting service")
    write_config(src_dir, "docker", port)
    wipe_docker()
    log("  Starting Docker service...", flush=True)
    rc = ts("start")
    if rc != 0:
        fail("ts start (Docker) returned non-zero")
        return
    if not wait_ready(port):
        fail("Docker service did not become ready")
        return

    section("Docker mode -- search_code")
    hits = check_search(port)
    if not hits:
        fail("(skipping filename + query_single_file tests -- no search results)")
        return

    section("Docker mode -- filename handling")
    rel = check_filename_docker(hits, src_dir)
    if not rel:
        return

    section("Docker mode -- query_single_file")
    if not wait_api_ready(api_port):
        fail("Management API not ready -- skipping query_single_file tests")
        return
    check_qsf_docker(api_port, rel, src_dir)


def wipe_wsl() -> None:
    """Hard-wipe WSL Typesense data directory via service.py index --resethard."""
    log("  Hard wipe: stopping WSL service and wiping data directory...", flush=True)
    ts("stop")
    time.sleep(2)
    # index --resethard wipes ~/.local/typesense/, reinstalls binary, re-indexes
    rc = ts("index", "--resethard")
    if rc != 0:
        log(f"  WARNING: wsl index --resethard returned {rc} (continuing)", flush=True)


def run_wsl(src_dir: str) -> None:
    port     = 8108
    api_port = port + 1

    section("WSL mode -- starting service")
    write_config(src_dir, "wsl", port)
    wipe_wsl()   # hard-wipe data dir and fresh-index via entrypoint

    # WSL2 uses NAT networking; localhost on Windows forwards to WSL2 but may
    # need a moment after a fresh start.  Get the WSL2 VM IP as a fallback so
    # we can reach Typesense directly even if the Windows localhost proxy lags.
    wsl_ip = get_wsl2_ip()
    log(f"  WSL2 IP: {wsl_ip or '(could not detect, using localhost)'}", flush=True)
    ts_host = wsl_ip if wsl_ip else "localhost"
    api_host = wsl_ip if wsl_ip else "localhost"

    if not wait_ready(port, timeout=30, host=ts_host):
        fail("WSL service did not become ready")
        return

    section("WSL mode -- search_code")
    hits = _wsl_search(ts_host, port, "IBenchmarkService")
    if not hits:
        fail("(skipping filename + query_single_file tests -- no search results)")
        return

    section("WSL mode -- filename handling")
    rel = check_filename_wsl(hits, src_dir)
    if not rel:
        return

    section("WSL mode -- query_single_file")
    if not _wait_api_ready_host(api_host, api_port):
        fail("Management API not ready -- skipping query_single_file tests")
        return
    check_qsf_wsl(api_host, api_port, rel, src_dir)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src_dir", help="Source directory to index (e.g. C:\\\\repos\\\\myproject)")
    ap.add_argument("--docker", action="store_true", help="Run Docker mode tests only")
    ap.add_argument("--wsl",    action="store_true", help="Run WSL mode tests only")
    args = ap.parse_args()

    run_docker_mode = args.docker or (not args.docker and not args.wsl)
    run_wsl_mode    = args.wsl    or (not args.docker and not args.wsl)

    src_dir = args.src_dir.rstrip("\\/")
    if not os.path.isdir(src_dir):
        print(f"ERROR: directory not found: {src_dir}")
        sys.exit(1)

    src_dir = src_dir.replace("\\", "/")

    _log_open()

    log(f"tscodesearch end-to-end mode test")
    log(f"  source dir : {src_dir}")
    log(f"  repo       : {SCRIPT_DIR}")
    log(f"  log file   : {LOG_PATH}")
    log(f"")
    log(f"  NOTE: this will temporarily overwrite config.json and stop any")
    log(f"        running codesearch service on port 8108.")

    # Back up original config
    orig = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else None

    modes = []
    if run_docker_mode: modes.append("docker")
    if run_wsl_mode:    modes.append("wsl")
    log(f"  modes      : {', '.join(modes)}")

    try:
        if run_docker_mode:
            run_docker(src_dir)
        if run_wsl_mode:
            run_wsl(src_dir)
    finally:
        section("Cleanup")
        log("  Stopping service...", flush=True)
        ts("stop")
        if orig is not None:
            CONFIG_FILE.write_text(orig, encoding="utf-8")
            log("  config.json restored.")
        else:
            CONFIG_FILE.unlink(missing_ok=True)
            log("  config.json removed (was not present before test).")
        _log_close()

    total  = _passed + _failed
    status = "ALL PASSED" if _failed == 0 else f"{_failed} FAILED"
    print(f"\n{'=' * 62}")
    print(f"  {_passed}/{total} passed  --  {status}")
    print(f"  log file: {LOG_PATH}")
    print(f"{'=' * 62}\n")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
