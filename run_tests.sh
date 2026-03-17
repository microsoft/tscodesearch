#!/usr/bin/env bash
# Run the codesearch test suite.
#
# Modes
# ──────
#   (default)   Run against an already-running Typesense.  If none is found,
#               download + start one automatically (like ci-test.sh).
#
#   --docker    Build the codesearch Docker image (if not already built),
#               start a container with sample/root1 and sample/root2 mounted,
#               wait for indexing to complete, run tests/test_sample_e2e.py
#               against it, then stop and remove the container.
#
# Flags
# ──────
#   --vscode      Force VS Code extension tests on (docker: default on, native: default off)
#   --no-vscode   Skip VS Code extension tests in both modes
#
# Examples (from Git Bash / Claude Code Bash tool)
# ────────────────────────────────────────────────
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh --docker
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh --docker --no-vscode
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh -k TestSample
#   MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh tests/test_sample_e2e.py
#
# Environment overrides (native mode)
#   CODESEARCH_PORT   default: read from config.json, else 8108
#   CODESEARCH_KEY    default: read from config.json, else codesearch-local
#   TYPESENSE_VERSION default: 27.1

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTEST="${PYTEST:-$HOME/.local/indexserver-venv/bin/pytest}"

# Source nvm so node/npm are available if installed via nvm (non-login shells won't have it)
NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck source=/dev/null
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
TYPESENSE_VERSION="${TYPESENSE_VERSION:-27.1}"
TS_DIR="${TS_DIR:-/tmp/typesense-ci}"

# ── Parse arguments ───────────────────────────────────────────────────────────

DOCKER_MODE=false
RUN_VSCODE=auto   # auto: on in docker, off in native; force on/off with --vscode/--no-vscode
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --docker)    DOCKER_MODE=true;         shift ;;
        --vscode)    RUN_VSCODE=true;          shift ;;
        --no-vscode) RUN_VSCODE=false;         shift ;;
        *)           EXTRA_ARGS+=("$1");       shift ;;
    esac
done

# ── Ensure pytest is available ────────────────────────────────────────────────

if [[ ! -x "$PYTEST" ]]; then
    echo "ERROR: pytest not found at $PYTEST" >&2
    echo "Run setup first: bash $REPO/codesearch-wsl.sh setup-venvs --repo-path $REPO" >&2
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# DOCKER MODE
# ─────────────────────────────────────────────────────────────────────────────

if $DOCKER_MODE; then

    IMAGE="codesearch-mcp"
    CONTAINER="codesearch-e2e-$$"
    DATA_VOL="codesearch_e2e_data_$$"
    E2E_TS_PORT=18108    # host-side Typesense port (container 8108)
    E2E_API_PORT=18109   # host-side management API port (container 8109), 127.0.0.1 only
    E2E_API_KEY="e2e-test-key"

    SAMPLE_ROOT1="$REPO/sample/root1"
    SAMPLE_ROOT2="$REPO/sample/root2"

    [[ -d "$SAMPLE_ROOT1" ]] || { echo "ERROR: sample/root1 not found at $SAMPLE_ROOT1" >&2; exit 1; }
    [[ -d "$SAMPLE_ROOT2" ]] || { echo "ERROR: sample/root2 not found at $SAMPLE_ROOT2" >&2; exit 1; }

    # Verify Docker is running
    docker info --format '{{.ID}}' >/dev/null 2>&1 \
        || { echo "ERROR: Docker is not running. Start Docker Desktop first." >&2; exit 1; }

    # Build image if it doesn't exist
    if ! docker images -q "$IMAGE" 2>/dev/null | grep -q .; then
        echo "==> Building Docker image '$IMAGE'..."
        docker build -t "$IMAGE" -f "$REPO/docker/Dockerfile" "$REPO"
        echo "==> Image built."
    else
        echo "==> Image '$IMAGE' already exists."
    fi

    # Write temp config.json (roots use container-internal paths /source/root1, /source/root2)
    TMP_CONFIG=$(mktemp /tmp/e2e-config-XXXXXX.json)
    cat > "$TMP_CONFIG" <<EOF
{
  "api_key": "$E2E_API_KEY",
  "port": 8108,
  "roots": {
    "root1": "/source/root1",
    "root2": "/source/root2"
  }
}
EOF

    cleanup_docker() {
        echo ""
        echo "==> Stopping Docker container '$CONTAINER'..."
        docker stop "$CONTAINER" 2>/dev/null || true
        docker rm   "$CONTAINER" 2>/dev/null || true
        docker volume rm "$DATA_VOL"         2>/dev/null || true
        rm -f "$TMP_CONFIG"
        echo "==> Cleanup done."
    }
    trap cleanup_docker EXIT

    echo "==> Starting container '$CONTAINER' (TS: $E2E_TS_PORT, mgmt: 127.0.0.1:$E2E_API_PORT)..."
    docker run -d --name "$CONTAINER" \
        -p "${E2E_TS_PORT}:8108" \
        -p "127.0.0.1:${E2E_API_PORT}:8109" \
        -e CODESEARCH_API_HOST=0.0.0.0 \
        -v "${SAMPLE_ROOT1}:/source/root1:ro" \
        -v "${SAMPLE_ROOT2}:/source/root2:ro" \
        -v "${TMP_CONFIG}:/app/codesearch/config.json:ro" \
        -v "${DATA_VOL}:/typesensedata" \
        "$IMAGE" \
        > /dev/null
    echo "==> Container started."

    # Wait for Typesense health
    echo -n "==> Waiting for Typesense health"
    for i in $(seq 1 60); do
        if curl -sf "http://localhost:${E2E_TS_PORT}/health" 2>/dev/null | grep -q '"ok":true'; then
            echo " OK (${i}s)"
            break
        fi
        if [[ $i -eq 60 ]]; then
            echo ""
            echo "ERROR: Typesense did not become healthy within 60s" >&2
            echo "Container logs:" >&2
            docker logs --tail 30 "$CONTAINER" >&2
            exit 1
        fi
        echo -n "."
        sleep 1
    done

    # Wait for both collections to have documents
    for ROOT_NAME in root1 root2; do
        COLL="codesearch_${ROOT_NAME}"
        COLL_URL="http://localhost:${E2E_TS_PORT}/collections/${COLL}"
        echo -n "==> Waiting for collection '$COLL' to have documents"
        for i in $(seq 1 90); do
            NDOCS=$(curl -sf -H "X-TYPESENSE-API-KEY: ${E2E_API_KEY}" "$COLL_URL" 2>/dev/null \
                    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('num_documents',0))" 2>/dev/null || echo 0)
            if [[ "${NDOCS:-0}" -ge 1 ]]; then
                echo " OK ($NDOCS docs)"
                break
            fi
            if [[ $i -eq 90 ]]; then
                echo ""
                echo "ERROR: Collection '$COLL' had no documents after 90s" >&2
                echo "Container logs:" >&2
                docker logs --tail 50 "$CONTAINER" >&2
                exit 1
            fi
            echo -n "."
            sleep 1
        done
    done

    # Run E2E tests against Docker Typesense
    echo "==> Running E2E tests against Docker (port ${E2E_TS_PORT})..."
    cd "$REPO"
    CODESEARCH_TEST_PORT="$E2E_TS_PORT" \
    CODESEARCH_TEST_KEY="$E2E_API_KEY" \
        "$PYTEST" "tests/test_sample_e2e.py" -v "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

    # ── VS Code extension tests (Docker mode) ─────────────────────────────────
    if [[ "$RUN_VSCODE" != "false" ]]; then
        VSCODE_DIR="$REPO/vscode-codesearch"
        if [[ -f "$VSCODE_DIR/package.json" ]] && command -v npm >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
            echo ""
            echo "==> Running VS Code extension tests against Docker (port ${E2E_TS_PORT})..."

            # Install/update deps in WSL home (avoids node_modules on /mnt/ NTFS)
            VSCODE_DEPS="$HOME/.local/vscode-codesearch-deps"
            if [[ ! -d "$VSCODE_DEPS/node_modules" ]]; then
                echo "==> Installing VS Code extension npm deps into $VSCODE_DEPS..."
                mkdir -p "$VSCODE_DEPS"
                cp "$VSCODE_DIR/package.json" "$VSCODE_DEPS/"
                [[ -f "$VSCODE_DIR/package-lock.json" ]] && cp "$VSCODE_DIR/package-lock.json" "$VSCODE_DEPS/"
                (cd "$VSCODE_DEPS" && npm ci --silent)
            fi

            # Write a config.json for the extension pointing at the Docker container
            # Note: management API (port+1) is not exposed from the container, so
            # uses/implements pipeline tests will skip — declarations still runs.
            TMP_EXT_CONFIG=$(mktemp /tmp/e2e-ext-config-XXXXXX.json)
            cat > "$TMP_EXT_CONFIG" <<EOF
{
  "api_key": "$E2E_API_KEY",
  "port": $E2E_TS_PORT,
  "roots": {
    "root1": "/source/root1",
    "root2": "/source/root2"
  }
}
EOF
            cd "$VSCODE_DIR"
            CS_CONFIG="$TMP_EXT_CONFIG" CS_QUERY="IProcessor" \
                NODE_PATH="$VSCODE_DEPS/node_modules" \
                node --require tsx/cjs --test \
                src/test/client.test.ts src/test/pipeline.test.ts
            cd "$REPO"
            rm -f "$TMP_EXT_CONFIG"
            echo "==> VS Code extension tests passed."
        else
            echo "==> Skipping VS Code extension tests (npm not found or vscode-codesearch/ missing)."
        fi
    fi

    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# NATIVE MODE — use existing Typesense or auto-start one
# ─────────────────────────────────────────────────────────────────────────────

# Read port and key from config.json if present
_CFG="$REPO/config.json"
if [[ -f "$_CFG" ]]; then
    _KEY="$(python3  -c "import json; d=json.load(open('$_CFG')); print(d.get('api_key',''))"  2>/dev/null || true)"
    _PORT="$(python3 -c "import json; d=json.load(open('$_CFG')); print(d.get('port',8108))"   2>/dev/null || true)"
fi
TS_PORT="${CODESEARCH_PORT:-${_PORT:-8108}}"
TS_KEY="${CODESEARCH_KEY:-${_KEY:-codesearch-local}}"

TS_PID=""
cleanup_native() {
    if [[ -n "$TS_PID" ]]; then
        echo ""
        echo "==> Stopping Typesense (pid $TS_PID)..."
        kill "$TS_PID" 2>/dev/null || true
    fi
}
trap cleanup_native EXIT

# Check if Typesense is already running
if curl -sf "http://localhost:${TS_PORT}/health" 2>/dev/null | grep -q '"ok":true'; then
    echo "==> Typesense already running on port ${TS_PORT}."
else
    # Download and start Typesense
    mkdir -p "$TS_DIR/data"
    TS_BIN="$TS_DIR/typesense-server"
    if [[ ! -x "$TS_BIN" ]]; then
        echo "==> Downloading Typesense ${TYPESENSE_VERSION}..."
        curl -fsSL \
            "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz" \
            | tar -xz -C "$TS_DIR"
        chmod +x "$TS_BIN"
    fi

    # Write a config.json if none exists (so indexserver.config can import)
    if [[ ! -f "$_CFG" ]]; then
        echo "==> Writing minimal config.json for tests..."
        cat > "$_CFG" <<EOF
{
  "port": ${TS_PORT},
  "api_key": "${TS_KEY}",
  "roots": { "default": "/tmp/src" }
}
EOF
    fi

    echo "==> Starting Typesense on port ${TS_PORT}..."
    "$TS_BIN" \
        --data-dir="$TS_DIR/data" \
        --api-key="$TS_KEY" \
        --api-port="$TS_PORT" \
        > "$TS_DIR/typesense.log" 2>&1 &
    TS_PID=$!

    echo -n "==> Waiting for Typesense"
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${TS_PORT}/health" 2>/dev/null | grep -q '"ok":true'; then
            echo " ready (${i}s)"
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo ""
            echo "ERROR: Typesense did not start in 30s. Log:" >&2
            tail -20 "$TS_DIR/typesense.log" >&2
            exit 1
        fi
        echo -n "."
        sleep 1
    done
fi

# If no test targets specified, run the full suite
if [[ ${#EXTRA_ARGS[@]} -eq 0 ]]; then
    EXTRA_ARGS=("tests/")
fi

echo "==> Running tests..."
cd "$REPO"
"$PYTEST" -v "${EXTRA_ARGS[@]}"

# ── VS Code extension tests (native mode, opt-in) ─────────────────────────────
if [[ "$RUN_VSCODE" == "true" ]]; then
    VSCODE_DIR="$REPO/vscode-codesearch"
    if [[ -f "$VSCODE_DIR/package.json" ]] && command -v npm >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
        echo ""
        echo "==> Running VS Code extension tests (native mode)..."
        VSCODE_DEPS="$HOME/.local/vscode-codesearch-deps"
        if [[ ! -d "$VSCODE_DEPS/node_modules" ]]; then
            echo "==> Installing VS Code extension npm deps into $VSCODE_DEPS..."
            mkdir -p "$VSCODE_DEPS"
            cp "$VSCODE_DIR/package.json" "$VSCODE_DEPS/"
            [[ -f "$VSCODE_DIR/package-lock.json" ]] && cp "$VSCODE_DIR/package-lock.json" "$VSCODE_DEPS/"
            (cd "$VSCODE_DEPS" && npm ci --silent)
        fi
        cd "$VSCODE_DIR"
        # CS_QUERY must be set by the caller or live tests skip
        NODE_PATH="$VSCODE_DEPS/node_modules" \
            node --require tsx/cjs --test \
            src/test/client.test.ts src/test/pipeline.test.ts
        cd "$REPO"
        echo "==> VS Code extension tests done."
    else
        echo "==> Skipping VS Code extension tests (npm not found or vscode-codesearch/ missing)."
    fi
fi
