#!/usr/bin/env bash
# Run the test suite the same way the GitHub Actions CI does.
# Works on Linux / WSL. Does NOT run test_docker.py (use Docker for that).
#
# Usage:
#   bash ci-test.sh               # all tests (except docker)
#   bash ci-test.sh -k TestIndexer  # pass extra pytest args
#
# Environment overrides:
#   TYPESENSE_VERSION   default: 27.1
#   TYPESENSE_API_KEY   default: ci-test-key
#   TYPESENSE_PORT      default: 8108
#   TS_DIR              default: /tmp/typesense-ci
#   VENV_DIR            default: /tmp/ci-venv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TYPESENSE_VERSION="${TYPESENSE_VERSION:-27.1}"
TS_DIR="${TS_DIR:-/tmp/typesense-ci}"
VENV_DIR="${VENV_DIR:-/tmp/ci-venv}"

# Read api_key and port from existing config.json if present, else use CI defaults
_CFG="$SCRIPT_DIR/config.json"
if [[ -f "$_CFG" ]]; then
    _KEY="$(python3 -c "import json,sys; d=json.load(open('$_CFG')); print(d.get('api_key','ci-test-key'))" 2>/dev/null || true)"
    _PORT="$(python3 -c "import json,sys; d=json.load(open('$_CFG')); print(d.get('port',8108))" 2>/dev/null || true)"
fi
TYPESENSE_API_KEY="${TYPESENSE_API_KEY:-${_KEY:-ci-test-key}}"
TYPESENSE_PORT="${TYPESENSE_PORT:-${_PORT:-8108}}"

cleanup() {
    if [[ -n "${TS_PID:-}" ]]; then
        echo "Stopping Typesense (pid $TS_PID)..."
        kill "$TS_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── 1. Python venv ─────────────────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$SCRIPT_DIR/requirements-dev.txt"

# ── 2. config.json ─────────────────────────────────────────────────────────
echo "Writing config.json (api_key=${TYPESENSE_API_KEY}, port=${TYPESENSE_PORT})..."
cat > "$SCRIPT_DIR/config.json" <<EOF
{
  "port": ${TYPESENSE_PORT},
  "api_key": "${TYPESENSE_API_KEY}",
  "roots": { "default": "/tmp/src" }
}
EOF

# ── 3. Typesense ────────────────────────────────────────────────────────────
if curl -sf "http://localhost:${TYPESENSE_PORT}/health" > /dev/null 2>&1; then
    echo "Typesense already running on port ${TYPESENSE_PORT}, reusing it."
else
    mkdir -p "$TS_DIR/data"
    TS_BIN="$TS_DIR/typesense-server"
    if [[ ! -x "$TS_BIN" ]]; then
        echo "Downloading Typesense ${TYPESENSE_VERSION}..."
        curl -fsSL \
            "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz" \
            | tar -xz -C "$TS_DIR"
        chmod +x "$TS_BIN"
    fi

    echo "Starting Typesense..."
    "$TS_BIN" \
        --data-dir="$TS_DIR/data" \
        --api-key="$TYPESENSE_API_KEY" \
        --api-port="$TYPESENSE_PORT" \
        > "$TS_DIR/typesense.log" 2>&1 &
    TS_PID=$!

    echo "Waiting for Typesense to be ready..."
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${TYPESENSE_PORT}/health" > /dev/null 2>&1; then
            echo "Typesense ready after ${i}s"
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "ERROR: Typesense did not start in 30s. Log:" >&2
            tail -20 "$TS_DIR/typesense.log" >&2
            exit 1
        fi
        sleep 1
    done
fi

# ── 4. Run tests ─────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/pytest" tests/ -v --tb=short --ignore=tests/test_docker.py "$@"
