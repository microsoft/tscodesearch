@echo off
:: VS Code extension unit tests for codesearch.
::
:: Usage:
::   run_tests.cmd
::
:: Python tests are run directly with pytest:
::   MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ query/tests/ -v"
node "%~dp0run_tests.mjs" %*
