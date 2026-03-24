#!/bin/bash
# Universal startup for codesearch indexserver.
#
# Docker mode (default, no flags):
#   Runs in foreground; process supervisor keeps container alive.
#   Config is read from CONFIG_FILE (mounted) or generated from env vars.
#
# WSL background mode (--background):
#   Called by service.py; starts daemons and exits.
#   Config must already exist at CONFIG_FILE.
#   Skips starting processes that are already running.

set -e

# ── Flags ─────────────────────────────────────────────────────────────────────

BACKGROUND=0
DISOWN=0
for arg in "$@"; do
    case "$arg" in
        --background)  BACKGROUND=1 ;;
        --disown)      DISOWN=1 ;;
    esac
done

# ── Configurable paths ────────────────────────────────────────────────────────
# Docker defaults are set here. WSL callers override via environment variables.

CODESEARCH_PORT="${CODESEARCH_PORT:-8108}"
CODESEARCH_ROOT_NAME="${CODESEARCH_ROOT_NAME:-default}"
CODESEARCH_API_KEY="${CODESEARCH_API_KEY:-}"
CODESEARCH_API_HOST="${CODESEARCH_API_HOST:-127.0.0.1}"

TYPESENSE_DATA="${TYPESENSE_DATA:-/typesensedata}"
TYPESENSE_DIR="${TYPESENSE_DIR:-}"         # Docker: /opt/typesense (set by Dockerfile ENV); WSL: empty

APP_ROOT="${APP_ROOT:-/app}"               # repo root inside Docker; WSL callers set to repo path
CONFIG_FILE="${CONFIG_FILE:-${APP_ROOT}/config.json}"
PYTHON3="${PYTHON3:-python3}"              # WSL callers set to ~/.local/indexserver-venv/bin/python3

TYPESENSE_LOG="${TYPESENSE_DATA}/typesense.log"
TYPESENSE_PID_FILE="${TYPESENSE_DATA}/typesense.pid"
API_PID_FILE="${TYPESENSE_DATA}/api.pid"

# ── Binary detection ──────────────────────────────────────────────────────────

if [ -n "$TYPESENSE_DIR" ] && [ -x "${TYPESENSE_DIR}/typesense-server" ]; then
    TYPESENSE_BIN="${TYPESENSE_DIR}/typesense-server"
elif [ -x "${HOME}/.local/typesense/typesense-server" ]; then
    TYPESENSE_BIN="${HOME}/.local/typesense/typesense-server"
else
    echo "[entrypoint] ERROR: Typesense binary not found."
    echo "             Set TYPESENSE_DIR to its directory, or install to ~/.local/typesense/typesense-server"
    exit 1
fi

# ── Helper: check if a PID file refers to a live process ─────────────────────

_check_running() {
    local pid_file="$1"
    local pid
    [ -f "$pid_file" ] || return 1
    pid=$(cat "$pid_file" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

# ── Config ────────────────────────────────────────────────────────────────────

if [ "$BACKGROUND" = "1" ]; then
    # WSL mode: config.json must already exist (created by setup or ts root add)
    if [ ! -f "$CONFIG_FILE" ] || [ ! -s "$CONFIG_FILE" ]; then
        echo "[entrypoint] ERROR: config.json not found at $CONFIG_FILE"
        echo "             Run setup.cmd to create it."
        exit 1
    fi
    echo "[entrypoint] Using config: $CONFIG_FILE"
    CODESEARCH_API_KEY=$("$PYTHON3" -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('api_key',''))")
    CODESEARCH_PORT=$("$PYTHON3" -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('port',8108))")
elif [ -f "$CONFIG_FILE" ] && [ -s "$CONFIG_FILE" ]; then
    # Docker extension-managed mode: config mounted read-only
    echo "[entrypoint] Using mounted config.json:"
    cat "$CONFIG_FILE"
    CODESEARCH_API_KEY=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('api_key',''))")
    CODESEARCH_PORT=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('port',8108))")
else
    # Docker standalone mode: generate config from environment variables
    if [ -z "$CODESEARCH_API_KEY" ]; then
        CODESEARCH_API_KEY="codesearch-$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)"
        echo "[entrypoint] Generated API key: $CODESEARCH_API_KEY"
    fi
    echo "[entrypoint] Generating config.json (standalone mode)..."
    cat > "$CONFIG_FILE" << EOF
{
    "api_key": "$CODESEARCH_API_KEY",
    "port": $CODESEARCH_PORT,
    "roots": {
        "$CODESEARCH_ROOT_NAME": {"local_path": "/source"}
    }
}
EOF
    echo "[entrypoint] Config written:"
    cat "$CONFIG_FILE"
fi

API_PORT=$((CODESEARCH_PORT + 1))

# ── Start Typesense ───────────────────────────────────────────────────────────

if [ "$BACKGROUND" = "1" ] && _check_running "$TYPESENSE_PID_FILE"; then
    echo "[entrypoint] Typesense already running (pid=$(cat "$TYPESENSE_PID_FILE"))"
else
    echo "[entrypoint] Starting Typesense on port $CODESEARCH_PORT..."
    mkdir -p "${TYPESENSE_DATA}/data"

    nohup "$TYPESENSE_BIN" \
        --data-dir="${TYPESENSE_DATA}/data" \
        --api-key="$CODESEARCH_API_KEY" \
        --api-port="$CODESEARCH_PORT" \
        --peering-port="$((CODESEARCH_PORT - 1))" \
        --listen-address=0.0.0.0 \
        --enable-cors \
        > "$TYPESENSE_LOG" 2>&1 &

    TYPESENSE_PID=$!
    [ "$BACKGROUND" = "1" ] && [ "$DISOWN" = "1" ] && disown "$TYPESENSE_PID"
    echo "$TYPESENSE_PID" > "$TYPESENSE_PID_FILE"
    echo "[entrypoint] Typesense started (pid=$TYPESENSE_PID)"
fi

# ── Start management API (api.py — watcher + heartbeat + verifier threads) ───
# api.py starts immediately and waits for Typesense internally (async init).

if [ "$BACKGROUND" = "1" ] && _check_running "$API_PID_FILE"; then
    echo "[entrypoint] Management API already running (pid=$(cat "$API_PID_FILE"))"
else
    echo "[entrypoint] Starting management API on ${CODESEARCH_API_HOST}:${API_PORT}..."
    cd "$APP_ROOT"
    nohup "$PYTHON3" "${APP_ROOT}/indexserver/api.py" \
        --host "$CODESEARCH_API_HOST" --port "$API_PORT" \
        > "${TYPESENSE_DATA}/api.log" 2>&1 &

    API_PID=$!
    [ "$BACKGROUND" = "1" ] && [ "$DISOWN" = "1" ] && disown "$API_PID"
    echo "$API_PID" > "$API_PID_FILE"
    echo "[entrypoint] Management API started (pid=$API_PID)"
fi

echo "[entrypoint] Management API on port $API_PORT"

# ── Wait for management API health ───────────────────────────────────────────

echo -n "[entrypoint] Waiting for management API"
API_HEALTH_URL="http://127.0.0.1:${API_PORT}/health"
MAX_WAIT=30
WAITED=0

while [ $WAITED -lt $MAX_WAIT ]; do
    if "$PYTHON3" "${APP_ROOT}/scripts/http_ok.py" "$API_HEALTH_URL" 2>/dev/null; then
        echo " ready"
        break
    fi
    echo -n "."
    sleep 1
    WAITED=$((WAITED + 1))
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo ""
    echo "[entrypoint] ERROR: Management API did not become healthy within ${MAX_WAIT}s"
    echo "[entrypoint] Log output:"
    cat "${TYPESENSE_DATA}/api.log"
    exit 1
fi


# ── Foreground mode: handle signals and keep alive (Docker) ───────────────────

if [ "$BACKGROUND" = "0" ]; then
    cleanup() {
        echo ""
        echo "[entrypoint] Shutting down..."

        if [ -f "$API_PID_FILE" ]; then
            kill "$(cat "$API_PID_FILE")" 2>/dev/null || true
            rm -f "$API_PID_FILE"
            echo "[entrypoint] Stopped management API"
        fi

        if [ -f "$TYPESENSE_PID_FILE" ]; then
            kill "$(cat "$TYPESENSE_PID_FILE")" 2>/dev/null || true
            rm -f "$TYPESENSE_PID_FILE"
            echo "[entrypoint] Stopped Typesense"
        fi

        echo "[entrypoint] Shutdown complete"
        exit 0
    }

    trap cleanup SIGTERM SIGINT

    echo "[entrypoint] Ready for connections"
    wait "$API_PID"
else
    echo "[entrypoint] Ready (background mode)"
fi
