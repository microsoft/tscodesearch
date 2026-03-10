#!/bin/bash
# Run query.py tests entirely inside WSL.
# Usage (from WSL):  bash /path/to/claudeskills/codesearch/test-query.sh
set -e

VENV=/tmp/ts-test-venv
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$VENV/bin/python"

# Create venv + install deps if needed
if ! "$PY" -c "import tree_sitter_c_sharp" 2>/dev/null; then
    echo "[setup] Creating venv at $VENV ..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet tree-sitter tree-sitter-c-sharp pytest
    echo "[setup] Done."
fi

echo ""
echo "Running pytest test_query_cs.py ..."
"$VENV/bin/pytest" "$SCRIPT_DIR/tests/test_query_cs.py" -v
