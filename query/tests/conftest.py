"""Shared fixtures for query/tests."""
from __future__ import annotations

import os
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_ROOT1 = os.path.join(_REPO, "sample", "root1")
SAMPLE_ROOT2 = os.path.join(_REPO, "sample", "root2")


@pytest.fixture(scope="session")
def sample_root1() -> str:
    return SAMPLE_ROOT1


@pytest.fixture(scope="session")
def sample_root2() -> str:
    return SAMPLE_ROOT2
