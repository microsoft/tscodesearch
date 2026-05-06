#!/bin/bash
# Universal startup for codesearch indexserver (Typesense only).
#
# WSL flags:
#   --background [--disown]        Start Typesense as a daemon and exit.
#                                  --disown: survive WSL session end.
#   --stop                         Stop a running Typesense instance.
#   --resethard                    Stop existing instance, wipe data dir,
#                                  reinstall binary, then start fresh.
#                                  Combine with --background [--disown].
#   --log [--indexer|--error] [-n N]
#                                  Tail the server, indexer, or error log.
#
# Docker mode (no flags):
#   Run Typesense in the foreground; process supervisor keeps container alive.
#   Config is read from CONFIG_FILE (mounted) or generated from env vars.
#
# Note: the management API (PORT+1) is owned by tsquery_server.py on Windows.
# This script only manages Typesense.

set -e

# ── Flags ─────────────────────────────────────────────────────────────────────

BACKGROUND=0
DISOWN=0
STOP=0
RESETHARD=0
LOG=0
LOG_INDEXER=0
LOG_ERROR=0
LOG_LINES=40

while [[ $# -gt 0 ]]; do
    case "$1" in
        --background) BACKGROUND=1 ;;
        --disown)     DISOWN=1 ;;
        --stop)       STOP=1 ;;
        --resethard)  RESETHARD=1 ;;
        --log)        LOG=1 ;;
        --indexer)    LOG_INDEXER=1 ;;
        --error)      LOG_ERROR=1 ;;
        -n)           shift; LOG_LINES="${1:-40}" ;;
        *)            echo "[entrypoint] Unknown flag: $1" >&2; exit 1 ;;
    esac
    shift
done

# ── Configurable paths ────────────────────────────────────────────────────────
# Docker defaults are set here; WSL callers override via environment variables.

CODESEARCH_PORT="${CODESEARCH_PORT:-8108}"
CODESEARCH_ROOT_NAME="${CODESEARCH_ROOT_NAME:-default}"
CODESEARCH_API_KEY="${CODESEARCH_API_KEY:-}"

TYPESENSE_DATA="${TYPESENSE_DATA:-/typesensedata}"
TYPESENSE_DIR="${TYPESENSE_DIR:-}"         # Docker: /opt/typesense (set by Dockerfile ENV); WSL: empty

APP_ROOT="${APP_ROOT:-/app}"               # repo root inside Docker; WSL callers set to repo path
CONFIG_FILE="${CONFIG_FILE:-${APP_ROOT}/config.json}"
PYTHON3="${PYTHON3:-python3}"              # WSL callers set to ~/.local/indexserver-venv/bin/python3

TYPESENSE_LOG="${TYPESENSE_DATA}/typesense.log"
TYPESENSE_PID_FILE="${TYPESENSE_DATA}/typesense.pid"

# ── Helper: check if a PID file refers to a live process ─────────────────────

_check_running() {
    local pid_file="$1"
    local pid
    [ -f "$pid_file" ] || return 1
    pid=$(cat "$pid_file" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

# ── Helper: stop a running Typesense instance ─────────────────────────────────

_stop_typesense() {
    if _check_running "$TYPESENSE_PID_FILE"; then
        local pid
        pid=$(cat "$TYPESENSE_PID_FILE" 2>/dev/null)
        echo "[entrypoint] Stopping Typesense (pid=$pid)..."
        kill "$pid" 2>/dev/null || true
        local deadline=$(( $(date +%s) + 10 ))
        while [ "$(date +%s)" -lt "$deadline" ]; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "[entrypoint] Force-killing Typesense (pid=$pid)..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 0.3
        fi
        rm -f "$TYPESENSE_PID_FILE"
        echo "[entrypoint] Typesense stopped."
    else
        pkill -f "typesense-server" 2>/dev/null \
            && echo "[entrypoint] Stopped (no PID file — used pkill)." \
            || echo "[entrypoint] Typesense not running."
        rm -f "$TYPESENSE_PID_FILE"
    fi
}

# ── Stop command ──────────────────────────────────────────────────────────────

if [ "$STOP" = "1" ]; then
    _stop_typesense
    exit 0
fi

# ── Log command ──────────────────────────────────────────────────────────────

if [ "$LOG" = "1" ]; then
    if [ "$LOG_INDEXER" = "1" ]; then
        LOG_FILE="${TYPESENSE_DATA}/indexer.log"
        LABEL="indexer"
    elif [ "$LOG_ERROR" = "1" ]; then
        LOG_FILE="${TYPESENSE_DATA}/typesense-error.log"
        LABEL="server error"
    else
        LOG_FILE="${TYPESENSE_DATA}/typesense.log"
        LABEL="server"
    fi
    if [ -f "$LOG_FILE" ]; then
        tail -n "$LOG_LINES" "$LOG_FILE"
    else
        echo "(no $LABEL log found at $LOG_FILE)"
    fi
    exit 0
fi

# ── Hard reset: stop + wipe + reinstall before binary detection ───────────────

if [ "$RESETHARD" = "1" ]; then
    _stop_typesense
    echo "[entrypoint] Wiping data directory: $TYPESENSE_DATA"
    rm -rf "$TYPESENSE_DATA"
    mkdir -p "$TYPESENSE_DATA"
    echo "[entrypoint] Reinstalling Typesense binary..."
    "$PYTHON3" "$APP_ROOT/indexserver/start_server.py" --install
fi

# ── Binary detection ──────────────────────────────────────────────────────────

if [ -n "$TYPESENSE_DIR" ] && [ -x "${TYPESENSE_DIR}/typesense-server" ]; then
    TYPESENSE_BIN="${TYPESENSE_DIR}/typesense-server"
elif [ -x "${TYPESENSE_DATA}/typesense-server" ]; then
    TYPESENSE_BIN="${TYPESENSE_DATA}/typesense-server"
elif [ -x "${HOME}/.local/typesense/typesense-server" ]; then
    TYPESENSE_BIN="${HOME}/.local/typesense/typesense-server"
else
    echo "[entrypoint] ERROR: Typesense binary not found."
    echo "             Set TYPESENSE_DIR to its directory, or install to ~/.local/typesense/typesense-server"
    exit 1
fi

# ── Config ────────────────────────────────────────────────────────────────────

if [ -f "$CONFIG_FILE" ] && [ -s "$CONFIG_FILE" ]; then
    # Config exists — read api_key and port from it (WSL and Docker with mounted config)
    echo "[entrypoint] Using config: $CONFIG_FILE"
    CODESEARCH_API_KEY=$("$PYTHON3" -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('api_key',''))")
    CODESEARCH_PORT=$("$PYTHON3" -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('port',8108))")
elif [ "$BACKGROUND" = "1" ]; then
    # WSL mode requires a config — it must be created by setup or 'ts root add'
    echo "[entrypoint] ERROR: config.json not found at $CONFIG_FILE"
    echo "             Run setup.cmd to create it."
    exit 1
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

# ── Wait for Typesense health (foreground/Docker only) ────────────────────────

if [ "$BACKGROUND" = "0" ]; then
    echo -n "[entrypoint] Waiting for Typesense"
    TS_HEALTH_URL="http://127.0.0.1:${CODESEARCH_PORT}/health"
    MAX_WAIT=60
    WAITED=0
    while [ $WAITED -lt $MAX_WAIT ]; do
        if "$PYTHON3" "${APP_ROOT}/scripts/http_ok.py" "$TS_HEALTH_URL" 2>/dev/null; then
            echo " ready"
            break
        fi
        echo -n "."
        sleep 1
        WAITED=$((WAITED + 1))
    done
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo ""
        echo "[entrypoint] ERROR: Typesense did not become healthy within ${MAX_WAIT}s"
        exit 1
    fi
fi

# ── Foreground mode: handle signals and keep alive (Docker) ───────────────────

if [ "$BACKGROUND" = "0" ]; then
    cleanup() {
        if [ -f "$TYPESENSE_PID_FILE" ]; then
            kill "$(cat "$TYPESENSE_PID_FILE")" 2>/dev/null || true
            rm -f "$TYPESENSE_PID_FILE"
        fi
        echo "[entrypoint] Stopped Typesense"
        exit 0
    }
    trap cleanup SIGTERM SIGINT
    echo "[entrypoint] Typesense ready"
    wait "$TYPESENSE_PID"
else
    echo "[entrypoint] Ready (background mode)"
fi
