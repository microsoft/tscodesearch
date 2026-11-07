@echo off
:: VS Code extension unit tests for codesearch.
::
:: Usage:
::   run_tests.cmd
::
:: Python tests use uv directly with the root .venv:
::   uv run --no-sync python -m pytest tests/ query/tests/ -v
node "%~dp0run_tests.mjs" %*
