@echo off
:: Register (or unregister) the codesearch MCP server with Claude Code.
::
:: Usage:
::   setup_mcp.cmd <src-dir>         -- install: write config.json, create WSL venvs, register MCP
::   setup_mcp.cmd --uninstall       -- unregister MCP server (venvs are left in place)
setlocal

set "REPO=%~dp0"
set "REPO=%REPO:~0,-1%"

:: ── Uninstall path ─────────────────────────────────────────────────────────
if /i "%~1"=="--uninstall" (
    echo Removing codesearch MCP server ...
    claude mcp remove --scope user tscodesearch
    if errorlevel 1 (
        echo WARNING: claude mcp remove failed ^(server may not have been registered^).
    ) else (
        echo Done. Restart Claude Code for the change to take effect.
    )
    goto :eof
)

:: ── Check WSL is installed and functional ──────────────────────────────────
where wsl.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: wsl.exe not found in PATH.
    echo        WSL is required to run the Typesense server.
    echo        Install WSL: wsl --install
    exit /b 1
)
wsl.exe --status >nul 2>&1
if errorlevel 1 (
    echo ERROR: WSL is installed but not functional ^(wsl --status failed^).
    echo        Try: wsl --install  or  wsl --update
    exit /b 1
)
:: Quick sanity-check: can WSL actually run a shell command?
wsl.exe bash -c "exit 0" >nul 2>&1
if errorlevel 1 (
    echo ERROR: WSL is installed but cannot run bash.
    echo        Ensure a Linux distribution is installed: wsl --install -d Ubuntu
    exit /b 1
)

:: ── Require src-dir argument ───────────────────────────────────────────────
if "%~1"=="" (
    echo Usage: setup_mcp.cmd ^<src-dir^> [api-key]
    echo   src-dir  Windows path to the source tree to index ^(e.g. C:\myproject\src^)
    echo   api-key  Typesense API key ^(optional; random 40-char hex key generated if omitted^)
    exit /b 1
)

set "SRC_DIR=%~1"

:: ── [1/3] Write config.json (first-time only) ─────────────────────────────
::
:: Config format (new - supports multiple named source roots):
::   {
::     "api_key": "<randomly generated 40-char hex key>",
::     "port": 8108,
::     "roots": {
::       "default": "C:/myproject/src",
::       "myother": "C:/other/src"
::     }
::   }
::
:: Legacy format (still supported - auto-promoted to roots.default at runtime):
::   { "src_root": "C:/myproject/src", "api_key": "<key>" }
::
:: To add more roots after setup: edit config.json, add entries under "roots",
:: then run: ts.cmd index --root <name> --reset
::
set "SRC_FWD=%SRC_DIR:\=/%"

:: Find a free port starting from 8108
set "PORT="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$p=8108; $used=([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()|ForEach-Object{$_.Port}); while($p -in $used){$p++}; $p"`) do set "PORT=%%P"
if "%PORT%"=="" set "PORT=8108"

:: ── [1/3] Write config.json + create WSL venvs (setup_mcp.sh handles both) ──
echo.
echo [1/3] WSL setup (config.json + venvs) via setup_mcp.sh ...
for /f "usebackq delims=" %%P in (`wsl.exe wslpath -u "%REPO%"`) do set "WSL_REPO=%%P"
if "%WSL_REPO%"=="" (
    echo ERROR: Could not convert repo path to WSL path.
    echo        Path attempted: %REPO%
    exit /b 1
)
wsl.exe bash "%WSL_REPO%/setup_mcp.sh" "%SRC_FWD%" "%PORT%" "%~2"
if errorlevel 1 (
    echo ERROR: WSL setup failed. See messages above.
    exit /b 1
)

:: ── [2/3] Register MCP ────────────────────────────────────────────────────
echo.
echo [2/3] Registering MCP server with Claude Code ...
claude mcp remove --scope user tscodesearch >nul 2>&1
claude mcp add --scope user tscodesearch -- wsl.exe bash -l "%WSL_REPO%/mcp.sh"
if errorlevel 1 (
    echo ERROR: Failed to register MCP server.
    exit /b 1
)

:: ── [3/3] Start indexserver ───────────────────────────────────────────────
echo.
echo [3/3] Starting indexserver ^(Typesense + watcher + indexer^) ...
call "%REPO%\ts.cmd" start
if errorlevel 1 (
    echo ERROR: Failed to start indexserver.
    echo        Check logs: ts.cmd log
    echo        Check logs: ts.cmd log --indexer
    exit /b 1
)

echo.
echo Done. Restart Claude Code for the change to take effect.
echo.
echo Indexing is running in the background. Monitor progress with:
echo   ts.cmd status                        -- server health + indexing progress
echo   ts.cmd log --indexer                 -- tail indexer log
echo.
echo Other commands:
echo   ts.cmd stop / restart                -- manage the indexserver
echo   ts.cmd index --reset                 -- re-index the default root from scratch
echo   ts.cmd index --root ^<name^> --reset  -- re-index a specific named root
endlocal
