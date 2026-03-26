#!/usr/bin/env bash
# run-wsl-tests.sh — all-in-one test runner for WSL mode.
#
# Runs the full test suite in a single WSL session:
#   1. ensure venv + Typesense binary
#   2. stop any leftover test-port processes from a previous run
#   3. start isolated Typesense + management API (session-attached; dies when script exits)
#   4. run pytest
#   5. run VS Code extension tests via node.exe (Windows interop) if available
#
# Non-destructive: uses CODESEARCH_PORT (default 18108) so the production
# instance on port 8108 is never touched.
#
# Usage:
#   bash run-wsl-tests.sh [--no-vscode] [pytest-args...]
#
# Required env vars:
#   TYPESENSE_VERSION   e.g. 27.1
#   TYPESENSE_DATA      data dir, e.g. /tmp/codesearch-wsl-test
#   CONFIG_FILE         path to test config.json (written by run_tests.mjs)
#   CODESEARCH_CONFIG   same as CONFIG_FILE; read by indexserver/config.py
#   CODESEARCH_PORT     Typesense port (should differ from production, e.g. 18108)
#   APP_ROOT            repo root (WSL path)
#   PYTEST              path to pytest binary
set -euo pipefail

: "${TYPESENSE_VERSION:?run-wsl-tests.sh: TYPESENSE_VERSION not set}"
: "${TYPESENSE_DATA:?run-wsl-tests.sh: TYPESENSE_DATA not set}"
: "${CONFIG_FILE:?run-wsl-tests.sh: CONFIG_FILE not set}"
: "${CODESEARCH_PORT:?run-wsl-tests.sh: CODESEARCH_PORT not set}"
: "${APP_ROOT:?run-wsl-tests.sh: APP_ROOT not set}"
: "${PYTEST:?run-wsl-tests.sh: PYTEST not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON3="${PYTEST%/bin/pytest}/bin/python3"

# Parse --no-vscode flag; remaining args forwarded to pytest.
RUN_VSCODE=1
PYTEST_ARGS=()
for _arg in "$@"; do
    if [ "$_arg" = "--no-vscode" ]; then
        RUN_VSCODE=0
    else
        PYTEST_ARGS+=("$_arg")
    fi
done

# ── Phase 1: venv + Typesense binary ──────────────────────────────────────────
bash "$SCRIPT_DIR/wsl-setup.sh"

# ── Phase 2: stop any previous test-port instance via PID files ───────────────
# Only kills processes recorded in our test data directory — never production.
_wait_for_pid_to_die() {
    local _pid="$1" _deadline=$((SECONDS + 10))
    while [ $SECONDS -lt $_deadline ]; do
        kill -0 "$_pid" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -9 "$_pid" 2>/dev/null || true
    sleep 0.2
}

for _pid_file in "$TYPESENSE_DATA/typesense.pid" "$TYPESENSE_DATA/api.pid"; do
    if [ -f "$_pid_file" ]; then
        _pid=$(cat "$_pid_file" 2>/dev/null || true)
        if [[ "$_pid" =~ ^[0-9]+$ ]] && kill -0 "$_pid" 2>/dev/null; then
            kill "$_pid" 2>/dev/null || true
            _wait_for_pid_to_die "$_pid"
        fi
        rm -f "$_pid_file"
    fi
done

# ── Phase 3: clean data dir + start services ──────────────────────────────────
rm -rf "$TYPESENSE_DATA"
mkdir -p "$TYPESENSE_DATA"

# Session-attached (no --disown): background processes die when this script exits.
PYTHON3="$PYTHON3" CODESEARCH_API_HOST=127.0.0.1 \
    bash "$SCRIPT_DIR/entrypoint.sh" --background

# ── Phase 4: pytest ───────────────────────────────────────────────────────────
export CODESEARCH_CONFIG="${CODESEARCH_CONFIG:-$CONFIG_FILE}"
cd "$APP_ROOT"
set +e
"$PYTEST" -v "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"
PYTEST_EXIT=$?
set -e

# ── Phase 5: VS Code extension tests (management API still up) ────────────────
VSCODE_EXIT=0
if [ "$RUN_VSCODE" = "1" ]; then
    VSCODE_DIR="$APP_ROOT/vscode-codesearch"
    echo ""
    echo "=== VSCODE_TESTS_START ==="
    if [ -d "$VSCODE_DIR/node_modules" ] && command -v node.exe &>/dev/null; then
        WIN_CFG=$(wslpath -w "$CONFIG_FILE")
        set +e
        (
            cd "$VSCODE_DIR"
            CS_CONFIG="$WIN_CFG" CS_QUERY=IProcessor CS_SUB=Processors.cs \
                node.exe --require tsx/cjs --test \
                    src/test/client.test.ts \
                    src/test/pipeline.test.ts
        ) 2>&1
        VSCODE_EXIT=$?
        set -e
    else
        echo "[skip] node.exe not found or node_modules missing"
    fi
    echo "=== VSCODE_TESTS_END ==="
fi

exit $((PYTEST_EXIT | VSCODE_EXIT))
