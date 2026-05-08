"""
Pytest session fixture for integration tests.

If ``CODESEARCH_CONFIG`` is already set in the environment the caller is
trusted to have set up a config. Otherwise a temporary ``config.json`` is
written pointing to the ``sample/`` fixture trees as ``root1`` and ``root2``.
Tantivy indexes are created on demand under ``<repo>/.tantivy/`` for each
test collection.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

# Ensure indexserver/ is importable before pytest_configure fires.
_REPO_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_BASE not in sys.path:
    sys.path.insert(0, _REPO_BASE)
from indexserver.config import normalize_path

_TEST_PORT: int = int(os.environ.get("CODESEARCH_TEST_PORT", 18108))
_TEST_KEY:  str = "codesearch-test"

# Repo root — two levels up from tests/integration/
_REPO: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_config_file: str | None = None


def pytest_configure(config) -> None:  # noqa: ANN001
    """Set up a temporary config before test collection."""
    global _config_file

    if os.environ.get("CODESEARCH_CONFIG"):
        return

    fd, _config_file = tempfile.mkstemp(
        prefix="codesearch-integration-", suffix=".json"
    )
    os.close(fd)
    _root1 = normalize_path(os.path.join(_REPO, "sample", "root1"))
    _root2 = normalize_path(os.path.join(_REPO, "sample", "root2"))
    _roots = {
        "root1": {"path": _root1},
        "root2": {"path": _root2},
    }
    with open(_config_file, "w", encoding="utf-8") as f:
        json.dump({"api_key": _TEST_KEY, "port": _TEST_PORT, "roots": _roots}, f)

    os.environ["CODESEARCH_CONFIG"] = _config_file


def pytest_unconfigure(config) -> None:  # noqa: ANN001
    """Remove the temp config after the session."""
    global _config_file
    if _config_file and os.path.exists(_config_file):
        os.unlink(_config_file)
        _config_file = None
