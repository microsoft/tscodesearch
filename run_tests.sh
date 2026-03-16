#!/usr/bin/env bash
# Run the full codesearch test suite.
# Usage:
#   bash run_tests.sh                  -- all tests
#   bash run_tests.sh -k TestQCasts    -- filter by name
#   bash run_tests.sh tests/test_mode_casts.py  -- single file
#
# From the Claude Code Bash tool (Git Bash), use:
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/q/spocore/tscodesearch/run_tests.sh [args...]

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTEST="$HOME/.local/indexserver-venv/bin/pytest"

exec "$PYTEST" "$REPO/tests/" -v "$@"
