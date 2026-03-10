#!/usr/bin/env bash
# Sets up all WSL-side dependencies for codesearch.
# Called automatically by setup_mcp.cmd; can also be run directly in WSL.
#
# Installs / updates:
#   ~/.local/mcp-venv/                   -- MCP client (mcp_server.py / mcp.sh)
#   ~/.local/indexserver-venv/           -- Indexserver (ts.sh / service.py / indexer.py)
#   ~/.local/typesense/typesense-server  -- Typesense search engine binary
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MCP_VENV="$HOME/.local/mcp-venv"
IDX_VENV="$HOME/.local/indexserver-venv"

# ── Optional args from setup_mcp.cmd: <src-dir-win> [port] [api-key] ─────────
_SRC_WIN="${1:-}"
_PORT="${2:-8108}"
_EXPLICIT_KEY="${3:-}"

# ── Read Typesense version from config.py (single source of truth) ────────────
TYPESENSE_VERSION=$(sed -n 's/^TYPESENSE_VERSION = "\(.*\)"/\1/p' \
    "$SCRIPT_DIR/indexserver/config.py")
if [ -z "$TYPESENSE_VERSION" ]; then
    echo "ERROR: Could not read TYPESENSE_VERSION from indexserver/config.py."
    echo "       Expected line: TYPESENSE_VERSION = \"<version>\""
    echo "       File: $SCRIPT_DIR/indexserver/config.py"
    exit 1
fi

# ── Verify prerequisites ───────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found in WSL."
    echo "       Fix:"
    echo "         sudo apt-get update"
    echo "         sudo apt-get install -y python3 python3-pip"
    exit 1
fi

# ensurepip is missing when the version-specific python3.X-venv package is absent
# (separate package on Debian/Ubuntu, e.g. python3.12-venv)
PY=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if ! python3 -c "import ensurepip" &>/dev/null 2>&1; then
    echo "ERROR: python3-venv not available (ensurepip missing for Python $PY)."
    echo "       Fix:"
    echo "         sudo apt-get update"
    echo "         sudo apt-get install -y python${PY}-venv"
    exit 1
fi

if ! command -v curl &>/dev/null; then
    echo "ERROR: curl not found in WSL."
    echo "       Fix:"
    echo "         sudo apt-get update"
    echo "         sudo apt-get install -y curl"
    exit 1
fi

echo "  Python $PY  |  Typesense $TYPESENSE_VERSION"

# ── Write config.json (first-time only; only when called with src-dir) ────────
_CONFIG_FILE="$SCRIPT_DIR/config.json"
if [ -n "$_SRC_WIN" ]; then
    echo ""
    echo "[1/4] Writing codesearch/config.json ..."
    if [ -f "$_CONFIG_FILE" ]; then
        echo "  config.json already exists (delete it to regenerate)."
    else
        _API_KEY="${_EXPLICIT_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(20))')}"
        python3 -c "
import json, sys
d = {'api_key': sys.argv[1], 'port': int(sys.argv[2]), 'roots': {'default': sys.argv[3]}}
print(json.dumps(d, indent=2))
" "$_API_KEY" "$_PORT" "$_SRC_WIN" > "$_CONFIG_FILE"
        echo "  root[default] = $_SRC_WIN"
        echo "  api_key       = $_API_KEY"
        echo "  port          = $_PORT"
    fi
fi

# ── [WSL 1/3] MCP client venv ─────────────────────────────────────────────────
echo ""
echo "[WSL 1/3] MCP venv: $MCP_VENV"
python3 -m venv "$MCP_VENV"
"$MCP_VENV/bin/pip" install --quiet --upgrade pip
if ! "$MCP_VENV/bin/pip" install --quiet --upgrade \
        mcp tree-sitter tree-sitter-c-sharp tree-sitter-python; then
    echo "ERROR: pip install failed for MCP venv."
    echo "       Check network connectivity or proxy settings and re-run setup_mcp.cmd."
    exit 1
fi
echo "  Done."

# ── [WSL 2/3] Indexserver venv ────────────────────────────────────────────────
echo ""
echo "[WSL 2/3] Indexserver venv: $IDX_VENV"
python3 -m venv "$IDX_VENV"
"$IDX_VENV/bin/pip" install --quiet --upgrade pip
if ! "$IDX_VENV/bin/pip" install --quiet --upgrade \
        typesense tree-sitter tree-sitter-c-sharp tree-sitter-python watchdog pathspec pytest; then
    echo "ERROR: pip install failed for indexserver venv."
    echo "       Check network connectivity or proxy settings and re-run setup_mcp.cmd."
    exit 1
fi
echo "  Done."

# ── [WSL 3/3] Typesense binary ────────────────────────────────────────────────
TYPESENSE_BIN="$HOME/.local/typesense/typesense-server"
TAR_URL="https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz"

echo ""
echo "[WSL 3/3] Typesense v${TYPESENSE_VERSION}: $TYPESENSE_BIN"
if [ -x "$TYPESENSE_BIN" ]; then
    echo "  Already installed, skipping download."
else
    echo "  Downloading from dl.typesense.org ..."
    mkdir -p "$HOME/.local/typesense"
    if ! curl -fL --progress-bar "$TAR_URL" | tar -xz -C "$HOME/.local/typesense/"; then
        echo "ERROR: Failed to download or extract Typesense v${TYPESENSE_VERSION}."
        echo "       URL: $TAR_URL"
        echo "       Check network connectivity and re-run setup_mcp.cmd."
        exit 1
    fi
    ACTUAL=$(find "$HOME/.local/typesense" -name 'typesense-server' -type f \
             2>/dev/null | head -1)
    if [ -z "$ACTUAL" ]; then
        echo "ERROR: typesense-server binary not found after extraction."
        echo "       The archive may have an unexpected layout."
        echo "       URL attempted: $TAR_URL"
        exit 1
    fi
    if [ "$ACTUAL" != "$TYPESENSE_BIN" ]; then
        mv "$ACTUAL" "$TYPESENSE_BIN"
    fi
    chmod +x "$TYPESENSE_BIN"
    echo "  Installed at $TYPESENSE_BIN"
fi
echo "  Done."
