#!/usr/bin/env bash
# Run the full codesearch test suite.
# Usage (from WSL): bash run_tests.sh [pytest args...]
# Example: bash run_tests.sh -k TestIndexQueue

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HOME/.local/indexserver-venv/bin/pytest"

cd "$SCRIPT_DIR"

exec "$VENV_PY" \
    tests/test_watcher.py \
    tests/test_indexer.py \
    tests/test_indexer_query_consistency.py \
    tests/test_verifier.py \
    tests/test_process_cs.py \
    tests/test_python.py \
    tests/test_query_cs.py \
    -v --tb=short \
    "$@"
