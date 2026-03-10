@echo off
:: Set up the indexserver venv in WSL at ~/.local/indexserver-venv.
:: Requires: WSL with Python 3.10+ installed (e.g. `wsl python3 --version`).
::
:: After setup, use ts.cmd to manage the service:
::   ts.cmd start           -- start Typesense + watcher + heartbeat
::   ts.cmd index --reset   -- index the default source root
::   ts.cmd status          -- check service health

echo Creating WSL venv at ~/.local/indexserver-venv ...
wsl bash -lc "python3 -m venv ~/.local/indexserver-venv"
if %errorlevel% neq 0 (
    echo ERROR: Failed to create venv.
    echo   Is Python 3.10+ available in WSL? Try: wsl python3 --version
    exit /b 1
)

echo Installing packages ^(typesense, tree-sitter, watchdog, pytest^) ...
wsl bash -lc "~/.local/indexserver-venv/bin/pip install --quiet --upgrade typesense tree-sitter tree-sitter-c-sharp watchdog pytest"
if %errorlevel% neq 0 (
    echo ERROR: pip install failed.
    exit /b 1
)

echo.
echo Done. WSL venv ready at ~/.local/indexserver-venv
echo.
echo Next steps:
echo   ts.cmd start                        -- start Typesense server + watcher
echo   ts.cmd index --reset               -- index the default source root
echo   ts.cmd status                      -- check service health
echo   run-server-tests.cmd               -- run server integration tests
