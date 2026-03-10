#!/bin/bash
# Self-locating MCP server launcher — no hardcoded paths.
# Registered via setup_mcp.cmd; works wherever the repo is cloned.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec ~/.local/mcp-venv/bin/python "$DIR/mcp_server.py"
