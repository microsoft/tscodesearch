"""Shared pytest fixtures and path setup for all tests/ subdirectories."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Computed directly from __file__ to avoid import-order bootstrapping issues.
REPO_ROOT = Path(__file__).parent.parent

# Add repo root to sys.path once so all test modules can import without per-file boilerplate.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    """Absolute Path to the repository root."""
    return REPO_ROOT
