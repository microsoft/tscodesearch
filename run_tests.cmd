@echo off
:: VS Code extension unit tests for codesearch.
::
:: Usage:
::   run_tests.cmd
::
:: Python tests are run directly with pytest from the client venv:
::   .client-venv\Scripts\python.exe -m pytest tests/ query/tests/ -v
node "%~dp0run_tests.mjs" %*
