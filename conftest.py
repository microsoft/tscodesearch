"""Root conftest.py — ensures config.json exists before test modules are imported.

indexserver/config.py is imported at collection time, so config.json must be
present before pytest starts collecting.  This hook writes a minimal default
when no config exists.  If config.json is already present (native run with a
live server, or Docker exec with a mounted config) it is left untouched.
"""
from __future__ import annotations

import json
import os
import tempfile

_REPO = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_REPO, "config.json")


def pytest_configure(config):
    if not os.path.exists(_CONFIG_PATH):
        _write_default_config()


def _write_default_config() -> None:
    data = {
        "port": 8108,
        "api_key": "codesearch-local",
        "roots": {"default": {"local_path": tempfile.gettempdir()}},
    }
    with open(_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)
