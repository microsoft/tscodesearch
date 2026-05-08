@echo off
:: Codesearch management CLI.
::
:: Usage:
::   ts start | stop | restart | status
::   ts verify [--root NAME] [--no-delete-orphans]
::   ts recreate [--root NAME]
::   ts log [-n N]
::   ts root | root --add NAME PATH | root --remove NAME
node.exe "%~dp0ts.mjs" %*
