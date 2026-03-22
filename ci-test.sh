#!/usr/bin/env bash
# Run the full codesearch CI test suite: native tests + Docker E2E.
#
# Stage 1 — Native tests
#   Downloads and starts a local Typesense (or reuses an already-running one),
#   runs the full pytest suite, then stops Typesense.
#
# Stage 2 — Docker E2E tests
#   Builds the codesearch-mcp image, starts a container with sample/root1 and
#   sample/root2 mounted, waits for indexing, runs test_sample_e2e.py against
#   the container, then stops and removes the container.
#   Skipped with a warning if Docker is not available.
#
# Usage:
#   bash ci-test.sh                    # full suite (native + Docker E2E)
#   bash ci-test.sh --no-docker        # native tests only
#   bash ci-test.sh -k TestIndexer     # extra pytest args forwarded to stage 1
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

# ── Parse args ────────────────────────────────────────────────────────────────

RUN_DOCKER=true
PYTEST_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-docker) RUN_DOCKER=false; shift ;;
        *)           PYTEST_ARGS+=("$1"); shift ;;
    esac
done

# ── Read api_key and port from existing config.json if present ────────────────

_CFG="$SCRIPT_DIR/config.json"
if [[ -f "$_CFG" ]]; then
    _KEY="$(python3 -c "import json; d=json.load(open('$_CFG')); print(d.get('api_key','ci-test-key'))" 2>/dev/null || true)"
    _PORT="$(python3 -c "import json; d=json.load(open('$_CFG')); print(d.get('port',8108))" 2>/dev/null || true)"
fi
TYPESENSE_API_KEY="${TYPESENSE_API_KEY:-${_KEY:-ci-test-key}}"
TYPESENSE_PORT="${TYPESENSE_PORT:-${_PORT:-8108}}"

TS_PID=""
cleanup() {
    if [[ -n "${TS_PID:-}" ]]; then
        echo "Stopping Typesense (pid $TS_PID)..."
        kill "$TS_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ═════════════════════════════════════════════════════════════════════════════
# STAGE 1 — Native pytest suite
# ═════════════════════════════════════════════════════════════════════════════

echo ""
echo "══════════════════════════════════════════"
echo " Stage 1: Native tests"
echo "══════════════════════════════════════════"

# ── 1a. Python venv ───────────────────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$SCRIPT_DIR/requirements-dev.txt"

# ── 1b. config.json ───────────────────────────────────────────────────────────
echo "Writing config.json (api_key=${TYPESENSE_API_KEY}, port=${TYPESENSE_PORT})..."
cat > "$SCRIPT_DIR/config.json" <<EOF
{
  "port": ${TYPESENSE_PORT},
  "api_key": "${TYPESENSE_API_KEY}",
  "roots": { "default": {"local_path": "/tmp/src"} }
}
EOF

# ── 1c. Start Typesense ───────────────────────────────────────────────────────
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

# ── 1d. Run native tests ──────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/pytest" tests/ -v --tb=short "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"

echo ""
echo "Stage 1 passed."

# ═════════════════════════════════════════════════════════════════════════════
# STAGE 2 — Docker E2E
# ═════════════════════════════════════════════════════════════════════════════

echo ""
echo "══════════════════════════════════════════"
echo " Stage 2: Docker E2E tests"
echo "══════════════════════════════════════════"

if ! $RUN_DOCKER; then
    echo "Skipped (--no-docker)."
    exit 0
fi

if ! docker info --format '{{.ID}}' >/dev/null 2>&1; then
    echo "WARNING: Docker not available — skipping Docker E2E tests." >&2
    echo "  To run them locally: bash run_tests.sh --docker" >&2
    exit 0
fi

# Delegate to run_tests.sh --docker (handles build, start, wait, test, cleanup)
# Pass VENV_DIR so it uses the same venv as stage 1.
HOME_PYTEST="$HOME/.local/indexserver-venv/bin/pytest"
if [[ -x "$HOME_PYTEST" ]]; then
    # run_tests.sh uses the indexserver-venv pytest; fine if it exists
    bash "$SCRIPT_DIR/run_tests.sh" --docker
else
    # Fall back to the CI venv
    PYTEST="$VENV_DIR/bin/pytest" bash "$SCRIPT_DIR/run_tests.sh" --docker
fi

echo ""
echo "Stage 2 passed."
echo ""
echo "══════════════════════════════════════════"
echo " All CI stages passed."
echo "══════════════════════════════════════════"
