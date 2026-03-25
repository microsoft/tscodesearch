#!/usr/bin/env bash
REPO="$(cd "$(dirname "$0")" && pwd)"
exec ~/.local/indexserver-venv/bin/python3 "$REPO/indexserver/smoke_test.py" "$@"
