#!/usr/bin/env bash
# Run the indexserver test suite.
# Usage: run-server-tests.sh [pytest-filter]
#   run-server-tests.sh                  -- all tests
#   run-server-tests.sh TestSearchFieldModes  -- specific class
#   run-server-tests.sh test_method_sigs      -- specific method
#
# Tests are split across thematic files in tests/:
#   test_indexer.py      — indexer, semantic fields, multi-root, extract_cs_metadata
#   test_watcher.py      — file watcher / change handler
#   test_process_cs.py   — process_file() C# structural query API
#   test_python.py       — Python metadata extraction and semantic fields
#   test_verifier.py     — index verifier (run_verify, _export_index)
REPO="$(cd "$(dirname "$0")" && pwd)"
FILTER="${1:-}"
if [[ -n "$FILTER" ]]; then
    exec ~/.local/indexserver-venv/bin/pytest "$REPO/tests/" -v -k "$FILTER"
else
    exec ~/.local/indexserver-venv/bin/pytest "$REPO/tests/" -v
fi
