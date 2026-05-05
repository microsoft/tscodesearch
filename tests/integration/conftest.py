"""
Pytest session fixture for integration tests.

Ensures an isolated Typesense instance is running before any test module is
imported or executed.

Behaviour
─────────
• If ``CODESEARCH_CONFIG`` is already set in the environment the caller is
  trusted to have started Typesense and this module does nothing.

• Otherwise a temporary ``config.json`` pointing to the test port
  (``CODESEARCH_TEST_PORT``, default 18108) is written and
  ``CODESEARCH_CONFIG`` is set in the environment *before* any test module is
  imported.  This means ``indexserver.config`` will read the test config rather
  than the production one.  The config includes ``root1`` and ``root2`` entries
  pointing to the ``sample/`` fixture trees in the repository.

• If Typesense is not already running on the test port the binary at
  ``~/.local/typesense/typesense-server`` is started in a fresh temporary data
  directory.  It is stopped and the temp files removed after the session.

• On Windows, running pytest directly is a no-op here — individual tests will
  skip via ``_assert_server_ok()``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request


_TEST_PORT: int = int(os.environ.get("CODESEARCH_TEST_PORT", 18108))
_TEST_KEY:  str = "codesearch-test"
_BIN_PATH:  str = os.path.expanduser("~/.local/typesense/typesense-server")

# Repo root — two levels up from tests/integration/
_REPO: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _to_config_path(p: str) -> str:
    """Convert an absolute path to the canonical config format.

    In WSL the server-local path is ``/mnt/x/...``; the config stores the
    Windows-style equivalent ``X:/...`` so that ``to_native_path()`` in
    ``indexserver/config.py`` can convert it back for file I/O.
    On native Linux the path is used as-is.
    """
    m = re.match(r"^/mnt/([a-zA-Z])(/.*)$", p)
    if m:
        return m.group(1).upper() + ":" + m.group(2)
    return p

# Mutable state — set during pytest_configure, cleaned up in pytest_unconfigure.
_ts_proc:    subprocess.Popen | None = None
_data_dir:   str | None = None
_config_file: str | None = None


def _ts_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health", timeout=2
        ) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def pytest_configure(config) -> None:  # noqa: ANN001
    """Called before test collection; set up Typesense for integration tests."""
    global _ts_proc, _data_dir, _config_file

    if sys.platform == "win32":
        return

    # Caller has already set up a config (e.g. manual test runs with custom config).
    if os.environ.get("CODESEARCH_CONFIG"):
        return

    # Write a temporary config pointing to the test port.
    fd, _config_file = tempfile.mkstemp(
        prefix="codesearch-integration-", suffix=".json"
    )
    os.close(fd)
    _root1 = os.path.join(_REPO, "sample", "root1")
    _root2 = os.path.join(_REPO, "sample", "root2")
    _roots = {
        "root1": {"path": _to_config_path(_root1)},
        "root2": {"path": _to_config_path(_root2)},
    }
    with open(_config_file, "w", encoding="utf-8") as f:
        json.dump({"api_key": _TEST_KEY, "port": _TEST_PORT, "roots": _roots}, f)

    # Must be set before any test module imports indexserver.config.
    os.environ["CODESEARCH_CONFIG"] = _config_file

    if _ts_healthy(_TEST_PORT):
        return  # already running (e.g. leftover from a previous run)

    if not os.path.isfile(_BIN_PATH):
        return  # binary not installed — tests will skip via _assert_server_ok()

    _data_dir = tempfile.mkdtemp(prefix="codesearch-integration-ts-")
    _ts_proc = subprocess.Popen(
        [
            _BIN_PATH,
            f"--data-dir={_data_dir}",
            f"--api-key={_TEST_KEY}",
            f"--listen-port={_TEST_PORT}",
            "--enable-cors",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait up to 30 s for Typesense to become healthy.
    deadline = time.time() + 30
    while time.time() < deadline:
        if _ts_healthy(_TEST_PORT):
            return
        if _ts_proc.poll() is not None:
            break  # process exited unexpectedly
        time.sleep(0.5)

    # Could not start — clean up so we don't leave a zombie.
    _ts_proc.kill()
    _ts_proc = None
    if _data_dir:
        shutil.rmtree(_data_dir, ignore_errors=True)
        _data_dir = None


def pytest_unconfigure(config) -> None:  # noqa: ANN001
    """Stop Typesense and remove temp files after the session."""
    global _ts_proc, _data_dir, _config_file

    if _ts_proc is not None:
        _ts_proc.terminate()
        try:
            _ts_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _ts_proc.kill()
        _ts_proc = None

    if _data_dir:
        shutil.rmtree(_data_dir, ignore_errors=True)
        _data_dir = None

    if _config_file and os.path.exists(_config_file):
        os.unlink(_config_file)
        _config_file = None
