@echo off
:: Quick smoke test for the Typesense index server.
:: Requires: server running (ts start) and indexserver venv set up (setup-indexserver.cmd).
set "_WIN=%~dp0"
for /f "usebackq tokens=*" %%W in (`wsl wslpath -u "%_WIN:~0,-1%"`) do set "_WSLDIR=%%W/"
wsl bash -l "%_WSLDIR%smoke-test.sh" %*
