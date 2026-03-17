"""Root conftest.py — pytest session setup for codesearch test suite.

Responsibilities
----------------
1. Write config.json before test modules are imported (pytest_configure runs
   before collection, so indexserver/config.py reads the right values).
2. Optionally start a Docker container for Typesense when --docker is passed,
   then tear it down after the session.

Usage
-----
Normal (expects Typesense already running on port 8108):
    pytest tests/

Point at an existing Typesense on a custom port/key:
    CODESEARCH_PORT=18108 CODESEARCH_API_KEY=mykey pytest tests/

Start Docker automatically and run everything against it:
    pytest tests/ --docker
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

_REPO = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_REPO, "config.json")

# Docker settings for --docker mode.
# Uses different host ports from test_docker.py (18108/13000) so both can
# coexist when the full suite (including test_docker.py) runs in one session.
_DOCKER_IMAGE = "codesearch-test:ci"
_DOCKER_API_KEY = "codesearch-ci-key"
_DOCKER_TS_PORT = 19108
_DOCKER_MCP_PORT = 19000

# State set by _start_docker, cleaned up in pytest_sessionfinish
_container_id: str = ""
_src_dir: str = ""


# ── pytest hooks ──────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--docker",
        action="store_true",
        default=False,
        help=(
            "Build the Docker image and start a container; run the full test "
            "suite against it. Tears down the container after the session."
        ),
    )


def pytest_configure(config):
    """Write config.json before any test module is imported."""
    use_docker = bool(config.getoption("--docker", default=False))

    if use_docker:
        try:
            _start_docker()
        except Exception as e:
            # Don't crash pytest with an INTERNALERROR. Write a fallback
            # config so modules can import; server-dependent tests will skip
            # themselves via @unittest.skipUnless(_server_ok(), ...).
            print(f"\n[conftest] WARNING: Docker setup failed — {e}", file=sys.stderr)
            print("[conftest] Integration tests will be skipped.", file=sys.stderr)
            _write_config(_DOCKER_TS_PORT, _DOCKER_API_KEY)
    elif os.environ.get("CODESEARCH_PORT"):
        port = int(os.environ["CODESEARCH_PORT"])
        api_key = os.environ.get("CODESEARCH_API_KEY", "codesearch-local")
        _write_config(port, api_key)
    elif not os.path.exists(_CONFIG_PATH):
        # No config.json at all — write a minimal default so modules can import.
        # Integration tests will be skipped if Typesense is not actually running.
        _write_config(8108, "codesearch-local")


def pytest_sessionfinish(session, exitstatus):
    global _container_id, _src_dir
    if _container_id:
        print(f"\n[conftest] Stopping container {_container_id[:12]}…", flush=True)
        subprocess.run(["docker", "stop", _container_id],
                       capture_output=True, timeout=30)
        subprocess.run(["docker", "rm", _container_id],
                       capture_output=True, timeout=10)
        _container_id = ""
    if _src_dir:
        shutil.rmtree(_src_dir, ignore_errors=True)
        _src_dir = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_config(port: int, api_key: str) -> None:
    data = {
        "port": port,
        "api_key": api_key,
        "roots": {"default": tempfile.gettempdir()},
    }
    with open(_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _wait_typesense(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/health", timeout=3
            ) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _docker_build() -> None:
    """Build the Docker image, retrying with --no-cache on layer-cache errors."""
    cmd = ["docker", "build", "-t", _DOCKER_IMAGE, "-f", "docker/Dockerfile", "."]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=_REPO, timeout=300)
    if r.returncode == 0:
        return

    # Corrupted layer cache produces "parent snapshot ... does not exist".
    # Retry once with --no-cache to recover.
    if "does not exist" in r.stderr or "snapshot" in r.stderr:
        print("[conftest] Layer cache error detected — retrying with --no-cache…",
              flush=True)
        r = subprocess.run(
            cmd + ["--no-cache"],
            capture_output=True, text=True, cwd=_REPO, timeout=300,
        )
        if r.returncode == 0:
            return

    raise RuntimeError(f"docker build failed:\n{r.stderr[-2000:]}")


def _start_docker() -> None:
    global _container_id, _src_dir

    print("\n[conftest] Building Docker image (this may take a minute)…", flush=True)
    _docker_build()

    # Minimal source dir to satisfy the volume mount
    _src_dir = tempfile.mkdtemp(prefix="ts_ci_src_")

    print(
        f"[conftest] Starting container "
        f"(Typesense → localhost:{_DOCKER_TS_PORT})…",
        flush=True,
    )
    r = subprocess.run(
        [
            "docker", "run", "-d",
            "-p", f"{_DOCKER_TS_PORT}:8108",
            "-p", f"{_DOCKER_MCP_PORT}:3000",
            "-v", f"{_src_dir}:/source:ro",
            "-e", f"CODESEARCH_API_KEY={_DOCKER_API_KEY}",
            "-e", "CODESEARCH_PORT=8108",
            "-e", "MCP_PORT=3000",
            _DOCKER_IMAGE,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker run failed:\n{r.stderr}")
    _container_id = r.stdout.strip()

    print(f"[conftest] Waiting for Typesense on :{_DOCKER_TS_PORT}…", flush=True)
    if not _wait_typesense(_DOCKER_TS_PORT):
        _dump_logs()
        raise RuntimeError(
            f"Typesense did not become healthy within 60s "
            f"(container {_container_id[:12]})"
        )
    print("[conftest] Typesense healthy.", flush=True)

    _write_config(_DOCKER_TS_PORT, _DOCKER_API_KEY)


def _dump_logs(tail: int = 40) -> None:
    if _container_id:
        r = subprocess.run(
            ["docker", "logs", "--tail", str(tail), _container_id],
            capture_output=True, text=True,
        )
        print(r.stdout + r.stderr, flush=True)
