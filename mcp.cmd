@echo off
:: Windows launcher for the codesearch MCP server.
:: Used by Claude Code extension (registered via setup_mcp.cmd).
"%~dp0.venv\Scripts\python.exe" "%~dp0mcp_server.py"
