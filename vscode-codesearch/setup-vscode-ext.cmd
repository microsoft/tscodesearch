@echo off
:: Setup and install the codesearch VS Code extension.
:: Run once after cloning, then reload VS Code.
::
:: Requires: VS Code (code on PATH), winget (built into Windows 11)

setlocal
pushd "%~dp0"

:: --- Ensure Node.js >= 20 ---
echo Checking Node.js...
node -e "process.exit(parseInt(process.version.slice(1)) >= 20 ? 0 : 1)" 2>nul
if not errorlevel 1 goto :node_ok

echo Node.js 20+ not found. Installing LTS via winget...
winget install --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :err_node

:: Add the standard Node install dir to this session's PATH
set "PATH=C:\Program Files\nodejs;%PATH%"

node -e "process.exit(parseInt(process.version.slice(1)) >= 20 ? 0 : 1)" 2>nul
if errorlevel 1 (
    echo Node not found at expected location. Close this terminal, reopen, and re-run.
    popd & exit /b 1
)

:node_ok
for /f "tokens=*" %%V in ('node --version') do echo Node.js %%V found.

echo.
echo [1/3] Installing npm dependencies...
call npm install --no-fund --no-audit
if errorlevel 1 goto :err

echo [2/3] Compiling TypeScript...
call npm run compile
if errorlevel 1 goto :err

echo [3/3] Packaging and installing extension...
call npm run package -- -o codesearch.vsix
if errorlevel 1 goto :err

call code --install-extension codesearch.vsix
if errorlevel 1 goto :err

if exist codesearch.vsix del codesearch.vsix
popd

echo.
echo Done! Reload VS Code and open "Code Search: Open Panel" with Ctrl+Shift+F1.
goto :eof

:err_node
popd
echo ERROR: winget failed to install Node.js. Install Node.js 20+ manually: https://nodejs.org
exit /b 1

:err
popd
echo.
echo ERROR: setup failed (see output above)
exit /b 1
