@echo off
:: Codesearch management CLI.
:: Delegates to ts.mjs (Node.js — no WSL required).
::
:: Usage:
::   ts start | stop | restart | status
::   ts index [--resethard] [--root NAME]
::   ts verify [--root NAME] [--no-delete-orphans]
::   ts log [-n N] [-f]
::   ts root | root --add NAME PATH | root --remove NAME
::   ts build
::   ts setup
node.exe "%~dp0ts.mjs" %*
