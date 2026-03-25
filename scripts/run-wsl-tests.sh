#!/usr/bin/env bash
# run-wsl-tests.sh — setup + start services + run pytest in one session.
#
# Non-destructive: starts an isolated Typesense on CODESEARCH_PORT (default 18108)
# using TYPESENSE_DATA as its data directory.  Never kills the production instance.
# Any previous test-port process is identified by PID file and stopped cleanly.
#
# Background processes are session-attached (no disown), so they are automatically
# cleaned up when this script exits.
#
# Required env vars:
#   TYPESENSE_VERSION   e.g. 27.1
#   TYPESENSE_DATA      data dir, e.g. /tmp/codesearch-wsl-test
#   CONFIG_FILE         path to test config.json
#   CODESEARCH_CONFIG   same as CONFIG_FILE; read by indexserver/config.py
#   CODESEARCH_PORT     Typesense port (should differ from production, e.g. 18108)
#   APP_ROOT            repo root (WSL path)
#   PYTEST              path to pytest binary
#
# Arguments are forwarded to pytest (e.g. -k TestFoo tests/test_foo.py)
set -euo pipefail

: "${TYPESENSE_VERSION:?run-wsl-tests.sh: TYPESENSE_VERSION not set}"
: "${TYPESENSE_DATA:?run-wsl-tests.sh: TYPESENSE_DATA not set}"
: "${CONFIG_FILE:?run-wsl-tests.sh: CONFIG_FILE not set}"
: "${CODESEARCH_PORT:?run-wsl-tests.sh: CODESEARCH_PORT not set}"
: "${APP_ROOT:?run-wsl-tests.sh: APP_ROOT not set}"
: "${PYTEST:?run-wsl-tests.sh: PYTEST not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON3="${PYTEST%/bin/pytest}/bin/python3"

# Phase 1: ensure venv + Typesense binary (non-destructive — no --reset)
bash "$SCRIPT_DIR/wsl-setup.sh"

# Phase 1b: stop any previous test-port instance using its PID files.
# We only kill processes recorded in our test data directory — never a
# production instance that may be running on a different port.
# Wait up to 10s for each process to actually exit before continuing.
_wait_for_pid_to_die() {
    local _pid="$1" _deadline=$((SECONDS + 10))
    while [ $SECONDS -lt $_deadline ]; do
        kill -0 "$_pid" 2>/dev/null || return 0   # process gone
        sleep 0.2
    done
    # Still alive — escalate to SIGKILL
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

# Phase 1c: wipe and recreate only the test data directory.
rm -rf "$TYPESENSE_DATA"
mkdir -p "$TYPESENSE_DATA"

# Phase 2: start Typesense + management API on the test port; blocks until healthy.
PYTHON3="$PYTHON3" CODESEARCH_API_HOST=127.0.0.1 \
    bash "$SCRIPT_DIR/entrypoint.sh" --background

# Phase 3: run pytest with CODESEARCH_CONFIG pointing at the test config so
# both config.py and indexserver/config.py connect to the test port.
export CODESEARCH_CONFIG="${CODESEARCH_CONFIG:-$CONFIG_FILE}"
cd "$APP_ROOT"
exec "$PYTEST" -v "$@"
