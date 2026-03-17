@echo off
:: Typesense service manager CLI for code search.
:: Wraps indexserver/service.py with the WSL indexserver venv.
::
:: Usage:
::   ts.cmd status
::   ts.cmd start
::   ts.cmd stop
::   ts.cmd restart
::   ts.cmd index [--reset] [--root <name>]
::   ts.cmd log [--indexer] [--heartbeat] [-n N]
::   ts.cmd watcher
::   ts.cmd heartbeat
set "_WIN=%~dp0"
for /f "usebackq tokens=*" %%W in (`wsl wslpath -u "%_WIN:~0,-1%"`) do set "_WSLDIR=%%W/"
wsl bash -l "%_WSLDIR%ts.sh" %*
if errorlevel 1 goto :eof

:: The Windows filesystem watcher is now built into the VS Code extension
:: (vscode-codesearch). It starts automatically when VS Code opens and sends
:: real-time file events to the indexserver via POST /file-events.
::
:: If you need the standalone watcher without VS Code (e.g. in CI or headless
:: setups), run win-watcher.cmd directly.
