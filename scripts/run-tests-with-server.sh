#!/usr/bin/env bash
# run-tests-with-server.sh — start Typesense + management API, then run a test command.
#
# Phases:
#   1. Download Typesense binary to TYPESENSE_DIR if absent (preserved across runs)
#   2. Kill any leftover processes from a previous run (via PID files in TYPESENSE_DATA)
#   3. Wipe TYPESENSE_DATA, start services via entrypoint.sh
#   4. Run the command passed as arguments; exit with its status
#
# Services use nohup so they survive this script's exit, allowing the caller
# (run_tests.mjs) to run VS Code tests against the still-running API.
# The next invocation of this script (Phase 2) kills them via PID files.
#
# Usage:
#   bash run-tests-with-server.sh <cmd> [args...]
#
# Required env vars:
#   TYPESENSE_VERSION   e.g. 27.1
#   TYPESENSE_DATA      test data dir (wiped each run), e.g. /tmp/codesearch-test/data
#   TYPESENSE_DIR       binary dir (preserved across runs), e.g. /tmp/codesearch-test/bin
#   CONFIG_FILE         path to test config.json (written by run_tests.mjs)
#   CODESEARCH_CONFIG   same as CONFIG_FILE; read by indexserver/config.py
#   CODESEARCH_PORT     Typesense port
#   APP_ROOT            repo root path
#   PYTEST              path to pytest binary (used to derive python3)

set -euo pipefail

: "${TYPESENSE_VERSION:?run-tests-with-server.sh: TYPESENSE_VERSION not set}"
: "${TYPESENSE_DATA:?run-tests-with-server.sh: TYPESENSE_DATA not set}"
: "${TYPESENSE_DIR:?run-tests-with-server.sh: TYPESENSE_DIR not set}"
: "${CONFIG_FILE:?run-tests-with-server.sh: CONFIG_FILE not set}"
: "${CODESEARCH_PORT:?run-tests-with-server.sh: CODESEARCH_PORT not set}"
: "${APP_ROOT:?run-tests-with-server.sh: APP_ROOT not set}"
: "${PYTEST:?run-tests-with-server.sh: PYTEST not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON3="${PYTEST%/bin/pytest}/bin/python3"

if [ ! -x "$PYTHON3" ]; then
    echo "[run-tests] ERROR: python3 not found at $PYTHON3"
    echo "  Run setup first (e.g. bash scripts/wsl-setup.sh)"
    exit 1
fi

# ── Phase 1: Typesense binary ─────────────────────────────────────────────────
mkdir -p "$TYPESENSE_DIR"
if [ ! -x "$TYPESENSE_DIR/typesense-server" ]; then
    echo "[run-tests] Downloading Typesense ${TYPESENSE_VERSION}..."
    curl -fsSL \
        "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz" \
        | tar -xz -C "$TYPESENSE_DIR"
    chmod +x "$TYPESENSE_DIR/typesense-server"
fi

# ── Phase 2: kill any previous test-port instance ────────────────────────────
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

# ── Phase 3: start services ───────────────────────────────────────────────────
rm -rf "$TYPESENSE_DATA"
mkdir -p "$TYPESENSE_DATA"

# Session-attached (no --disown): nohup keeps processes alive after this script
# exits so the caller can run additional tests against the running API.
PYTHON3="$PYTHON3" \
TYPESENSE_DIR="$TYPESENSE_DIR" \
CODESEARCH_API_HOST=127.0.0.1 \
    bash "$SCRIPT_DIR/entrypoint.sh" --background

# ── Phase 4: run test command ─────────────────────────────────────────────────
export CODESEARCH_CONFIG="${CODESEARCH_CONFIG:-$CONFIG_FILE}"
cd "$APP_ROOT"
set +e
"$@"
_exit=$?
set -e
exit $_exit
