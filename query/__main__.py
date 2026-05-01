"""
AST query CLI for a single file. Output is always JSON.

CLI mode:
    python -m query --mode methods --file C:/repos/myproject/src/Widget.cs
    python -m query --mode calls --pattern SaveChanges --file C:/repos/myproject/src/Widget.cs
    python -m query --mode uses --pattern IRepository --uses-kind param --file C:/repos/myproject/src/Widget.cs

JSON stdin mode:
    echo {"mode":"methods","file":"C:/repos/myproject/src/Widget.cs"} | python -m query --json

Output: {"matches": [{"line": N, "text": "..."}, ...]}
        {"error": "..."}  on failure
"""

import argparse
import json
import os
import sys

from .dispatch import query_file


def _run(mode, file_path, pattern="", include_body=False,
         symbol_kind=None, uses_kind=None):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        with open(file_path, "rb") as f:
            src_bytes = f.read()
    except OSError as e:
        return {"error": str(e)}
    matches = query_file(
        src_bytes, ext, mode, pattern,
        include_body=include_body,
        symbol_kind=symbol_kind or None,
        uses_kind=uses_kind or None,
    )
    return {"matches": matches}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true",
                    help="Read request as JSON from stdin instead of CLI args")
    ap.add_argument("--mode",         default="")
    ap.add_argument("--file",         default="")
    ap.add_argument("--pattern",      default="")
    ap.add_argument("--include-body", action="store_true")
    ap.add_argument("--symbol-kind",  default="")
    ap.add_argument("--uses-kind",    default="")
    args = ap.parse_args()

    if args.json:
        try:
            req = json.load(sys.stdin)
        except Exception as e:
            json.dump({"error": f"bad input: {e}"}, sys.stdout)
            sys.exit(1)
        result = _run(
            mode=req.get("mode", ""),
            file_path=req.get("file", ""),
            pattern=req.get("pattern", ""),
            include_body=req.get("include_body", False),
            symbol_kind=req.get("symbol_kind", ""),
            uses_kind=req.get("uses_kind", ""),
        )
    else:
        if not args.file:
            ap.error("--file is required")
        if not args.mode:
            ap.error("--mode is required")
        result = _run(
            mode=args.mode,
            file_path=args.file,
            pattern=args.pattern,
            include_body=args.include_body,
            symbol_kind=args.symbol_kind,
            uses_kind=args.uses_kind,
        )

    if result.get("error"):
        json.dump(result, sys.stdout)
        sys.exit(1)
    json.dump(result, sys.stdout)


main()
