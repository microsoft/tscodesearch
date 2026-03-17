#!/bin/bash
# Docker entrypoint for codesearch MCP + management API server.
#
# Supports two modes:
#
#   Extension-managed (recommended):
#     The VS Code extension generates config.json (multi-root) and mounts it
#     read-only at /app/codesearch/config.json.  All roots listed in config are
#     indexed on first run.  The management API is exposed on port PORT+1 so
#     the extension can poll status, receive file-change events, and trigger
#     verify passes.
#
#   Standalone (docker-compose):
#     No config.json is mounted.  Config is generated from environment variables
#     with a single root at /source.

set -e

# ── Default environment ───────────────────────────────────────────────────────

CODESEARCH_PORT="${CODESEARCH_PORT:-8108}"
CODESEARCH_ROOT_NAME="${CODESEARCH_ROOT_NAME:-default}"
CODESEARCH_API_KEY="${CODESEARCH_API_KEY:-}"
MCP_PORT="${MCP_PORT:-3000}"

export MCP_TRANSPORT="sse"
export MCP_PORT

TYPESENSE_DATA="${TYPESENSE_DATA:-/typesensedata}"
TYPESENSE_DIR="${TYPESENSE_DIR:-/opt/typesense}"

CONFIG_FILE="/app/codesearch/config.json"
TYPESENSE_BIN="${TYPESENSE_DIR}/typesense-server"
TYPESENSE_LOG="${TYPESENSE_DATA}/typesense.log"
TYPESENSE_PID_FILE="${TYPESENSE_DATA}/typesense.pid"
API_PID_FILE="${TYPESENSE_DATA}/api.pid"

# ── Config ────────────────────────────────────────────────────────────────────

if [ -f "$CONFIG_FILE" ] && [ -s "$CONFIG_FILE" ]; then
    echo "[entrypoint] Using mounted config.json:"
    cat "$CONFIG_FILE"
    # Read api_key and port from the mounted config
    CODESEARCH_API_KEY=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('api_key',''))")
    CODESEARCH_PORT=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d.get('port',8108))")
else
    # Standalone mode — generate config from environment variables
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
        "$CODESEARCH_ROOT_NAME": "/source"
    }
}
EOF
    echo "[entrypoint] Config written:"
    cat "$CONFIG_FILE"
fi

API_PORT=$((CODESEARCH_PORT + 1))

# ── Start Typesense ───────────────────────────────────────────────────────────

echo "[entrypoint] Starting Typesense on port $CODESEARCH_PORT..."
mkdir -p "${TYPESENSE_DATA}/data"

$TYPESENSE_BIN \
    --data-dir="${TYPESENSE_DATA}/data" \
    --api-key="$CODESEARCH_API_KEY" \
    --port="$CODESEARCH_PORT" \
    --enable-cors \
    > "$TYPESENSE_LOG" 2>&1 &

TYPESENSE_PID=$!
echo "$TYPESENSE_PID" > "$TYPESENSE_PID_FILE"
echo "[entrypoint] Typesense started (pid=$TYPESENSE_PID)"

# ── Wait for Typesense health ─────────────────────────────────────────────────

echo -n "[entrypoint] Waiting for Typesense"
HEALTH_URL="http://localhost:${CODESEARCH_PORT}/health"
MAX_WAIT=60
WAITED=0

while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
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
    echo "[entrypoint] Log output:"
    cat "$TYPESENSE_LOG"
    exit 1
fi

# ── Initial indexing (per root, skipped if collection already exists) ─────────

cd /app
PYTHONPATH=/app python3 - << 'PYEOF'
import json, re, subprocess, sys, urllib.request, os

with open('/app/codesearch/config.json') as f:
    cfg = json.load(f)

api_key = cfg.get('api_key', '')
port    = cfg.get('port', 8108)
roots   = cfg.get('roots', {})

for name, src_path in roots.items():
    coll = 'codesearch_' + re.sub(r'[^a-z0-9]', '_', name.lower())
    url  = f'http://localhost:{port}/collections/{coll}'
    try:
        req = urllib.request.Request(url, headers={'X-TYPESENSE-API-KEY': api_key})
        urllib.request.urlopen(req)
        print(f'[entrypoint] Collection "{coll}" already exists — skipping initial index', flush=True)
    except Exception:
        print(f'[entrypoint] Indexing root "{name}" ({src_path})...', flush=True)
        env = {**os.environ, 'PYTHONPATH': '/app'}
        r = subprocess.run(
            ['python3', '-u', 'codesearch/indexserver/indexer.py',
             '--src', src_path, '--collection', coll, '--resethard'],
            cwd='/app', env=env,
        )
        if r.returncode != 0:
            print(f'[entrypoint] ERROR: indexing failed for root "{name}"', file=sys.stderr, flush=True)
            sys.exit(1)
        print(f'[entrypoint] Indexing complete for "{name}"', flush=True)

print('[entrypoint] All roots ready', flush=True)
PYEOF

# ── Start management API (api.py — watcher + heartbeat + verifier threads) ───

CODESEARCH_API_HOST="${CODESEARCH_API_HOST:-127.0.0.1}"
echo "[entrypoint] Starting management API on ${CODESEARCH_API_HOST}:${API_PORT}..."
cd /app
PYTHONPATH=/app python3 codesearch/indexserver/api.py \
    --host "$CODESEARCH_API_HOST" --port "$API_PORT" \
    > "${TYPESENSE_DATA}/api.log" 2>&1 &

API_PID=$!
echo "$API_PID" > "$API_PID_FILE"
echo "[entrypoint] Management API started (pid=$API_PID)"

# ── Handle signals ────────────────────────────────────────────────────────────

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

# ── Start MCP server (foreground) ────────────────────────────────────────────

echo "[entrypoint] Starting MCP server on port $MCP_PORT..."
echo "[entrypoint] Management API on port $API_PORT"
echo "[entrypoint] Ready for connections"

cd /app
exec python3 codesearch/mcp_server.py
