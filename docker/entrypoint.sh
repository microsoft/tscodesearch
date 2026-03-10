#!/bin/bash
# Docker entrypoint for codesearch MCP server
#
# This script:
# 1. Generates config.json from environment variables
# 2. Starts the Typesense server
# 3. Waits for Typesense to be healthy
# 4. Runs initial indexing if the collection doesn't exist
# 5. Starts the file watcher in the background
# 6. Starts the MCP server (SSE transport)

set -e

# ── Configuration ────────────────────────────────────────────────────────────

CODESEARCH_PORT="${CODESEARCH_PORT:-8108}"
CODESEARCH_ROOT_NAME="${CODESEARCH_ROOT_NAME:-default}"
CODESEARCH_API_KEY="${CODESEARCH_API_KEY:-}"
MCP_PORT="${MCP_PORT:-3000}"

# Export for MCP server
export MCP_TRANSPORT="sse"
export MCP_PORT

# Generate API key if not provided
if [ -z "$CODESEARCH_API_KEY" ]; then
    CODESEARCH_API_KEY="codesearch-$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)"
    echo "[entrypoint] Generated API key: $CODESEARCH_API_KEY"
fi

TYPESENSE_DATA="${TYPESENSE_DATA:-/typesensedata}"
TYPESENSE_DIR="${TYPESENSE_DIR:-/opt/typesense}"

CONFIG_FILE="/app/codesearch/config.json"
TYPESENSE_BIN="${TYPESENSE_DIR}/typesense-server"
TYPESENSE_LOG="${TYPESENSE_DATA}/typesense.log"
TYPESENSE_PID_FILE="${TYPESENSE_DATA}/typesense.pid"
WATCHER_PID_FILE="${TYPESENSE_DATA}/watcher.pid"

# ── Generate config.json ─────────────────────────────────────────────────────

echo "[entrypoint] Generating config.json..."
cat > "$CONFIG_FILE" << EOF
{
    "api_key": "$CODESEARCH_API_KEY",
    "port": $CODESEARCH_PORT,
    "roots": {
        "$CODESEARCH_ROOT_NAME": "/source"
    }
}
EOF

echo "[entrypoint] Config written to $CONFIG_FILE"
cat "$CONFIG_FILE"

# ── Start Typesense ──────────────────────────────────────────────────────────

echo "[entrypoint] Starting Typesense server..."
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

# ── Wait for Typesense to be healthy ─────────────────────────────────────────

echo -n "[entrypoint] Waiting for Typesense health check"
HEALTH_URL="http://localhost:${CODESEARCH_PORT}/health"
MAX_WAIT=60
WAITED=0

while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
        echo ""
        echo "[entrypoint] Typesense is healthy!"
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

# ── Check if indexing is needed ──────────────────────────────────────────────

COLLECTION_NAME="codesearch_${CODESEARCH_ROOT_NAME}"
COLLECTION_URL="http://localhost:${CODESEARCH_PORT}/collections/${COLLECTION_NAME}"

echo "[entrypoint] Checking if collection '${COLLECTION_NAME}' exists..."

if curl -s -H "X-TYPESENSE-API-KEY: $CODESEARCH_API_KEY" "$COLLECTION_URL" 2>/dev/null | grep -q '"name"'; then
    echo "[entrypoint] Collection exists, skipping initial indexing"
else
    echo "[entrypoint] Collection not found, running initial indexing..."
    echo "[entrypoint] This may take a while for large codebases..."

    cd /app
    PYTHONPATH=/app python -u codesearch/indexserver/indexer.py \
        --src /source \
        --collection "$COLLECTION_NAME" \
        --reset 2>&1 | tee "${TYPESENSE_DATA}/indexer.log" | head -100

    echo "[entrypoint] Initial indexing complete"
fi

# ── Start file watcher ───────────────────────────────────────────────────────

echo "[entrypoint] Starting file watcher..."
cd /app
PYTHONPATH=/app python codesearch/indexserver/watcher.py \
    --src /source \
    --collection "$COLLECTION_NAME" \
    > "${TYPESENSE_DATA}/watcher.log" 2>&1 &

WATCHER_PID=$!
echo "$WATCHER_PID" > "$WATCHER_PID_FILE"
echo "[entrypoint] File watcher started (pid=$WATCHER_PID)"

# ── Handle signals ───────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "[entrypoint] Shutting down..."

    if [ -f "$WATCHER_PID_FILE" ]; then
        kill "$(cat "$WATCHER_PID_FILE")" 2>/dev/null || true
        rm -f "$WATCHER_PID_FILE"
        echo "[entrypoint] Stopped watcher"
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

# ── Start MCP server ─────────────────────────────────────────────────────────

echo "[entrypoint] Starting MCP server..."
echo "[entrypoint] MCP endpoint: http://0.0.0.0:${MCP_PORT}/sse"
echo "[entrypoint] Ready for connections"

cd /app
exec python codesearch/mcp_server.py
