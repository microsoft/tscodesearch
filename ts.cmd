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

:: After a successful start/restart, launch the Windows filesystem watcher in a
:: new console window. It watches Windows-path roots (e.g. Q:/...) using native
:: ReadDirectoryChangesW events and forwards changes to the indexserver in real time.
:: watcher.mjs exits immediately if no Windows-path roots are configured.
if /i "%~1"=="start"   start "codesearch win-watcher" "%_WIN%win-watcher.cmd"
if /i "%~1"=="restart" start "codesearch win-watcher" "%_WIN%win-watcher.cmd"
