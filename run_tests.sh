#!/usr/bin/env bash
# Run the full codesearch test suite.
#
# Usage:
#   bash run_tests.sh                  -- all tests (Typesense must be running)
#   bash run_tests.sh --docker         -- start Docker, run all tests, stop Docker
#   bash run_tests.sh -k TestQCasts    -- filter by name
#   bash run_tests.sh tests/test_mode_casts.py  -- single file
#
# Point at an already-running Typesense on a custom port:
#   CODESEARCH_PORT=18108 CODESEARCH_API_KEY=mykey bash run_tests.sh
#
# From the Claude Code Bash tool (Git Bash), use:
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/<drive>/path/to/tscodesearch/run_tests.sh [args...]

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTEST="$HOME/.local/indexserver-venv/bin/pytest"

exec "$PYTEST" "$REPO/tests/" -v "$@"
