@echo off
:: Test runner for codesearch.
::
:: Usage:
::   run_tests.cmd --wsl                          run all tests in WSL (isolated, non-destructive)
::   run_tests.cmd --wsl -k TestVerifier          filter by test class/method
::   run_tests.cmd --wsl tests/test_indexer.py   single file
::   run_tests.cmd --docker                       run all tests in Docker
::   run_tests.cmd --linux                        run all tests on native Linux
node "%~dp0run_tests.mjs" %*
