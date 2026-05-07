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
#   --diag                         Run startup diagnostics and print what
#                                  is wrong (binary, config, ports, locks,
#                                  data dir, HTTP health). Exits 0 if OK,
#                                  1 if any check failed.
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
DIAG=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --background) BACKGROUND=1 ;;
        --disown)     DISOWN=1 ;;
        --stop)       STOP=1 ;;
        --resethard)  RESETHARD=1 ;;
        --log)        LOG=1 ;;
        --indexer)    LOG_INDEXER=1 ;;
        --error)      LOG_ERROR=1 ;;
        --diag)       DIAG=1 ;;
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
    kill -0 "$pid" 2>/dev/null || return 1
    # Verify the live process is actually typesense-server (prevents stale PID
    # collisions after WSL restart, where the PID may be reused by a different process).
    grep -ql "typesense" /proc/"$pid"/cmdline 2>/dev/null
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

# ── Diag: check what might prevent Typesense from starting ────────────────────

if [ "$DIAG" = "1" ]; then
    set +e
    _PASS=0; _WARN=0; _FAIL=0
    _ok()   { printf '  [OK]  %s\n' "$*"; _PASS=$((_PASS+1)); }
    _warn() { printf '  [~~]  %s\n' "$*"; _WARN=$((_WARN+1)); }
    _fail() { printf '  [!!]  %s\n' "$*"; _FAIL=$((_FAIL+1)); }
    _info() { printf '        %s\n' "$*"; }
    _head() { printf '\n[diag] %s\n' "$*"; }

    printf '[diag] ── Typesense startup diagnostics ─────────────────────────────\n'
    printf '[diag]    data dir : %s\n' "$TYPESENSE_DATA"
    printf '[diag]    config   : %s\n' "$CONFIG_FILE"
    printf '[diag]    python   : %s\n' "$PYTHON3"

    # ── 1. Binary ──────────────────────────────────────────────────────────
    _head "1/7  Binary"
    _DIAG_BIN=""
    for _cand in \
        "${TYPESENSE_DIR:+${TYPESENSE_DIR}/typesense-server}" \
        "${TYPESENSE_DATA}/typesense-server" \
        "${HOME}/.local/typesense/typesense-server"
    do
        [ -n "$_cand" ] && [ -x "$_cand" ] && { _DIAG_BIN="$_cand"; break; }
    done
    if [ -z "$_DIAG_BIN" ]; then
        _fail "typesense-server not found or not executable"
        _info "Checked: \${TYPESENSE_DATA}/typesense-server, ~/.local/typesense/typesense-server"
        _info "Fix: run setup.cmd (installs the binary)"
    else
        _VER=$("$_DIAG_BIN" --version 2>&1 | grep -oE 'Typesense [0-9.]+' | head -1)
        _ok "$_DIAG_BIN  ${_VER:-(version unknown)}"
    fi

    # ── 2. Config ──────────────────────────────────────────────────────────
    _head "2/7  Config"
    _DIAG_PORT=""
    _DIAG_KEY=""
    if [ ! -f "$CONFIG_FILE" ]; then
        _fail "Not found: $CONFIG_FILE"
        _info "Fix: run setup.cmd to create config.json"
    elif [ ! -s "$CONFIG_FILE" ]; then
        _fail "File is empty: $CONFIG_FILE"
    else
        _DIAG_PORT=$("$PYTHON3" -c \
            "import json,sys; d=json.load(open('$CONFIG_FILE')); print(d['port'])" 2>/dev/null)
        _DIAG_KEY=$("$PYTHON3" -c \
            "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('api_key',''))" 2>/dev/null)
        if [ -z "$_DIAG_PORT" ]; then
            _fail "Could not read 'port' from config (missing key or Python error)"
            _info "Config : $CONFIG_FILE"
            _info "Python : $PYTHON3"
            "$PYTHON3" -c "import json" 2>/dev/null \
                || _info "(Python interpreter may be missing or broken)"
        else
            _ok "$CONFIG_FILE  (port=$_DIAG_PORT  key=${_DIAG_KEY:0:8}...)"
            [ -z "$_DIAG_KEY" ] && _warn "api_key is empty in config"
        fi
    fi
    _DIAG_PORT="${_DIAG_PORT:-${CODESEARCH_PORT}}"

    # ── 3. Data directory ──────────────────────────────────────────────────
    _head "3/7  Data directory"
    if [ ! -e "$TYPESENSE_DATA" ]; then
        _warn "Not found: $TYPESENSE_DATA  (will be created on first start)"
    elif [ ! -d "$TYPESENSE_DATA" ]; then
        _fail "$TYPESENSE_DATA exists but is not a directory"
    else
        _ok "$TYPESENSE_DATA"
        if [ ! -w "$TYPESENSE_DATA" ]; then
            _fail "Not writable — check permissions"
            _info "Owner: $(stat -c '%U:%G  mode %a' "$TYPESENSE_DATA" 2>/dev/null)"
            _info "Fix: sudo chown -R \$USER '$TYPESENSE_DATA'"
        fi
        _DF=$(df -m "$TYPESENSE_DATA" 2>/dev/null | awk 'NR==2{print $4}')
        if [ -n "$_DF" ]; then
            if   [ "$_DF" -lt 200  ]; then _fail "Very low disk space: ${_DF} MB free"
            elif [ "$_DF" -lt 1024 ]; then _warn "Low disk space: ${_DF} MB free"
            else _info "Disk free: ${_DF} MB"
            fi
        fi
    fi

    # ── 4. RocksDB lock ────────────────────────────────────────────────────
    _head "4/7  RocksDB lock"
    _LOCK="${TYPESENSE_DATA}/data/db/LOCK"
    if [ ! -f "$_LOCK" ]; then
        _ok "No lock file"
    else
        _LOCK_HOLDER=""
        if   command -v lsof  &>/dev/null; then
            _LOCK_HOLDER=$(lsof  "$_LOCK" 2>/dev/null | awk 'NR>1{print $1,"(pid "$2")"}' | head -1)
        elif command -v fuser &>/dev/null; then
            _LOCK_HOLDER=$(fuser "$_LOCK" 2>/dev/null)
        fi
        if [ -n "$_LOCK_HOLDER" ]; then
            _ok "Lock held by: $_LOCK_HOLDER  (Typesense is running)"
        else
            _ok "Lock file present, no current holder (normal post-shutdown state)"
            _info "RocksDB will reacquire the fcntl lock on next start — does not block startup."
        fi
    fi

    # ── 5. PID file and process ────────────────────────────────────────────
    _head "5/7  Process"
    if [ ! -f "$TYPESENSE_PID_FILE" ]; then
        _info "No PID file — Typesense is not running (or was cleanly stopped)"
    else
        _DIAG_PID=$(cat "$TYPESENSE_PID_FILE" 2>/dev/null)
        if [[ "$_DIAG_PID" =~ ^[0-9]+$ ]]; then
            if kill -0 "$_DIAG_PID" 2>/dev/null; then
                if grep -ql "typesense" /proc/"$_DIAG_PID"/cmdline 2>/dev/null; then
                    _ok "Typesense running (pid=$_DIAG_PID)"
                else
                    _CMD=$(tr '\0' ' ' </proc/"$_DIAG_PID"/cmdline 2>/dev/null | cut -c1-60)
                    _fail "PID $_DIAG_PID is alive but is NOT typesense-server: $_CMD"
                    _info "Stale PID file from a previous WSL session."
                    _info "Fix: rm '$TYPESENSE_PID_FILE'"
                fi
            else
                _ok "PID file present, pid=$_DIAG_PID is not running (typesense was killed or crashed)"
                _info "Does not block startup — 'ts start' will overwrite the PID file."
            fi
        else
            _fail "PID file contains invalid value: '$_DIAG_PID'"
            _info "Fix: rm '$TYPESENSE_PID_FILE'"
        fi
    fi

    # ── 6. Port ────────────────────────────────────────────────────────────
    _head "6/7  Port"
    _PORT_HOLDER=""
    if command -v ss &>/dev/null; then
        _PORT_HOLDER=$(ss -tlnp 2>/dev/null \
            | awk -v p=":${_DIAG_PORT} " '$0 ~ p || $0 ~ p"$" {print $NF}' \
            | grep -oP 'pid=\K[0-9]+' | head -1)
    fi
    if [ -n "$_PORT_HOLDER" ]; then
        _PNAME=$(cat /proc/"$_PORT_HOLDER"/comm 2>/dev/null || echo "?")
        _ok "Port ${_DIAG_PORT} in use by: $_PNAME (pid=$_PORT_HOLDER)"
    else
        _info "Port ${_DIAG_PORT} is not in use — Typesense is not listening"
    fi

    # ── 7. HTTP health ─────────────────────────────────────────────────────
    _head "7/7  HTTP health"
    _HEALTH_URL="http://127.0.0.1:${_DIAG_PORT}/health"
    if command -v curl &>/dev/null; then
        _HTTP_CODE=$(curl -s -o /tmp/_ts_diag_body \
            -w '%{http_code}' --max-time 3 "$_HEALTH_URL" 2>/dev/null)
        _HTTP_BODY=$(cat /tmp/_ts_diag_body 2>/dev/null)
        rm -f /tmp/_ts_diag_body
        case "$_HTTP_CODE" in
            200) _ok   "HTTP 200  $_HEALTH_URL  →  $_HTTP_BODY" ;;
            503) _warn "HTTP 503  $_HEALTH_URL  →  still loading  ($_HTTP_BODY)" ;;
            000|"") _fail "No response from $_HEALTH_URL — Typesense is not running" ;;
            *)   _warn "HTTP $_HTTP_CODE  $_HEALTH_URL  →  $_HTTP_BODY" ;;
        esac
    else
        _info "(curl not available — skipping HTTP check)"
    fi

    # ── Recent log ─────────────────────────────────────────────────────────
    printf '\n[diag] Recent log  (%s)\n' "$TYPESENSE_LOG"
    if [ -f "$TYPESENSE_LOG" ] && [ -s "$TYPESENSE_LOG" ]; then
        tail -15 "$TYPESENSE_LOG" | while IFS= read -r _line; do printf '  %s\n' "$_line"; done
    elif [ -f "$TYPESENSE_LOG" ]; then
        printf '  (empty — Typesense may have crashed before writing anything)\n'
        printf '  Check: binary missing? port conflict? RocksDB lock? (see checks above)\n'
    else
        printf '  (no log file yet)\n'
    fi

    # ── Summary ────────────────────────────────────────────────────────────
    printf '\n[diag] ─────────────────────────────────────────────────────────────\n'
    printf '[diag] %d passed  %d warnings  %d failed\n' "$_PASS" "$_WARN" "$_FAIL"
    if [ "$_FAIL" -gt 0 ]; then
        printf '[diag] Fix the issues above, then run: ts start\n'
        exit 1
    elif [ "$_WARN" -gt 0 ]; then
        printf '[diag] Ready to start (with warnings).\n'
    else
        printf '[diag] Everything looks good.\n'
    fi
    exit 0
fi

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
        --snapshot-interval-seconds=600 \
        > "$TYPESENSE_LOG" 2>&1 &

    TYPESENSE_PID=$!
    [ "$BACKGROUND" = "1" ] && [ "$DISOWN" = "1" ] && disown "$TYPESENSE_PID"
    echo "$TYPESENSE_PID" > "$TYPESENSE_PID_FILE"
    echo "[entrypoint] Typesense started (pid=$TYPESENSE_PID)"

    # Watch for early crash in background mode (foreground does its own health wait).
    # nohup returns instantly even if the binary dies, so a stable pid is meaningless
    # without a brief survival check. If it dies, dump the log so the user sees why.
    if [ "$BACKGROUND" = "1" ]; then
        for _ in 1 2 3; do
            sleep 1
            if ! kill -0 "$TYPESENSE_PID" 2>/dev/null; then
                echo "[entrypoint] ERROR: Typesense exited during startup (pid=$TYPESENSE_PID)"
                # glog tags error/fatal lines with E/F prefix — those are the root cause.
                # The tail then provides shutdown context for cases without glog output.
                _ERR_LINES=$(grep -E '^[EF][0-9]' "$TYPESENSE_LOG" 2>/dev/null | head -10)
                if [ -n "$_ERR_LINES" ]; then
                    echo "[entrypoint] ---- error/fatal lines from $TYPESENSE_LOG ----"
                    echo "$_ERR_LINES"
                fi
                echo "[entrypoint] ---- last 30 lines of $TYPESENSE_LOG ----"
                tail -n 30 "$TYPESENSE_LOG" 2>/dev/null || echo "(log empty or missing)"
                echo "[entrypoint] ---- end of log ----"
                rm -f "$TYPESENSE_PID_FILE"
                exit 1
            fi
        done
    fi
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
