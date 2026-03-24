#!/usr/bin/env python3
"""Exit 0 if GET <url> returns JSON with {"ok": true}, else exit 1.

Usage:
    python3 http_ok.py <url>
"""
import json
import sys
import urllib.request


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <url>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        sys.exit(0 if data.get("ok") is True else 1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
