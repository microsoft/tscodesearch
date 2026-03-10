@echo off
:: Launches the codesearch Windows filesystem watcher in this console.
:: Called by "ts start" automatically when Windows-path roots are configured.
:: Can also be run manually.
title codesearch win-watcher
cd /d "%~dp0win-watcher"
if not exist node_modules (
    echo Installing npm dependencies...
    call npm install
    if errorlevel 1 (
        echo ERROR: npm install failed. Make sure Node.js is installed.
        pause
        exit /b 1
    )
)
node watcher.mjs
pause
