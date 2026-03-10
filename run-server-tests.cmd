@echo off
:: Run the Typesense server tests using the indexserver venv.
:: Requires: Typesense server running (ts start), and WSL venv set up (setup-indexserver.cmd).
::
:: Usage:
::   run-server-tests.cmd                       -- run all server tests
::   run-server-tests.cmd TestSearchFieldModes  -- run a specific test class
::   run-server-tests.cmd test_method_sigs      -- run a specific test method
set "_WIN=%~dp0"
for /f "usebackq tokens=*" %%W in (`wsl wslpath -u "%_WIN:~0,-1%"`) do set "_WSLDIR=%%W/"
wsl bash -l "%_WSLDIR%run-server-tests.sh" %*
