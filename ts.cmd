@echo off
:: Codesearch management CLI.
::
:: Usage:
::   ts start | stop | restart | status
::   ts index [--resethard] [--root NAME]
::   ts verify [--root NAME] [--no-delete-orphans]
::   ts log [-n N]
::   ts root | root --add NAME PATH | root --remove NAME
node.exe "%~dp0ts.mjs" %*
