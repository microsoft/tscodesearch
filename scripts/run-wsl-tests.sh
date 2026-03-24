#!/usr/bin/env bash
# run-wsl-tests.sh — setup + start services + run pytest in one session.
#
# Background processes started by entrypoint.sh are session-attached (no disown),
# so they are automatically cleaned up when this script exits.
#
# Required env vars:
#   TYPESENSE_VERSION   e.g. 27.1
#   TYPESENSE_DATA      data dir, e.g. /tmp/codesearch-wsl-test
#   CONFIG_FILE         path to test config.json
#   CODESEARCH_PORT     Typesense port
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

# Phase 1: venv + binary + kill existing processes + wipe data dir
bash "$SCRIPT_DIR/wsl-setup.sh" --reset

# Phase 2: start Typesense + management API; blocks until both are healthy
PYTHON3="$PYTHON3" CODESEARCH_API_HOST=127.0.0.1 \
    bash "$SCRIPT_DIR/entrypoint.sh" --background

# Phase 3: run pytest
cd "$APP_ROOT"
exec "$PYTEST" -v "$@"
