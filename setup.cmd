@echo off
:: One-time codesearch setup.
:: Usage:
::   setup.cmd              Docker mode (default; requires Docker Desktop)
::   setup.cmd --wsl        WSL mode
::   setup.cmd --uninstall  Unregister MCP server and stop service

:: -- Ensure Node.js 20+ ----------------------------------------------------------
node -e "process.exit(parseInt(process.version.slice(1)) >= 20 ? 0 : 1)" 2>nul
if not errorlevel 1 goto :run

echo Node.js 20+ not found. Installing LTS via winget...
winget install --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
if errorlevel 1 ( echo ERROR: winget failed. Install Node.js 20+ manually then re-run. & exit /b 1 )
set "PATH=C:\Program Files\nodejs;%PATH%"
node -e "process.exit(parseInt(process.version.slice(1)) >= 20 ? 0 : 1)" 2>nul
if errorlevel 1 ( echo Node not found after install. Close terminal, reopen, and re-run. & exit /b 1 )

:run
node "%~dp0setup.mjs" %*
