@echo off
:: Quick smoke test for the Typesense index server.
:: Requires: server running (ts start) and indexserver venv set up (setup.cmd).
set "_WIN=%~dp0"
for /f "usebackq tokens=*" %%W in (`wsl wslpath -u "%_WIN:~0,-1%"`) do set "_WSLDIR=%%W/"
wsl bash -lc "~/.local/indexserver-venv/bin/python3 '%_WSLDIR%indexserver/smoke_test.py'" %*
