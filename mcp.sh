#!/bin/bash
# Self-locating uv-backed MCP server launcher.
# Registered via setup.cmd; works wherever the repo is cloned.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export UV_PROJECT_ENVIRONMENT="$DIR/.venv"
exec uv run --no-sync --project "$DIR" --directory "$DIR" python "$DIR/mcp_server.py"
