@echo off
setlocal
:: Windows launcher for the Python MCP server.
:: Registered with Claude Code via setup.cmd.
set "UV_PROJECT_ENVIRONMENT=%~dp0.venv"
uv run --no-sync --project "%~dp0." --directory "%~dp0." python "%~dp0mcp_server.py"
