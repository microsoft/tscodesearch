#!/usr/bin/env bash
# WSL wrapper — calls indexserver/service.py via the WSL indexserver venv.
# Run from within WSL (the repo is mounted at e.g. /mnt/c/myproject/claudeskills).
# Usage: ts.sh <command> [options]
REPO="$(cd "$(dirname "$0")" && pwd)"
exec ~/.local/indexserver-venv/bin/python3 "$REPO/indexserver/service.py" "$@"
