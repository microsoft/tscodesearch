@echo off
:: Test runner for codesearch.
::
:: Usage:
::   run_tests.cmd --wsl --destructive               run all tests in WSL (ERASES WSL index)
::   run_tests.cmd --wsl --destructive -k TestVerifier  filter by test class/method
::   run_tests.cmd --wsl --destructive tests/test_indexer.py  single file
::   run_tests.cmd --docker               run all tests in Docker
::   run_tests.cmd --linux                run all tests on native Linux
node "%~dp0run_tests.mjs" %*
