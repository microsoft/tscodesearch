"""Replace common non-ASCII codepoints in source files with ASCII equivalents.

Scope: ``.py`` / ``.md`` / ``.mjs`` / ``.js`` under the repo root, skipping
vendored / generated / fixture trees. Only handles the codepoints actually
present in the codebase (see scripts/find_nonascii.py for the inventory).

Dry-run by default -- pass ``--apply`` to actually write files.
"""
from __future__ import annotations

import argparse
import os
import sys

SKIP_DIRS = {
    ".tantivy", ".git", "node_modules", "sample", ".client-venv",
    "__pycache__", ".pytest_cache", "vscode-codesearch",
}

# Codepoint -> ASCII replacement. Order doesn't matter; ``str.translate`` does
# them all in one pass on a single-pass mapping table.
REPLACEMENTS: dict[int, str] = {
    0x2014: "--",   # -- em dash
    0x2013: "-",    # - en dash
    0x2500: "-",    # - box drawings light horizontal
    0x2550: "=",    # = box drawings double horizontal
    0x2502: "|",    # | box drawings light vertical
    0x251c: "|-",   # |-
    0x2524: "-|",   # -|
    0x2514: "`-",   # `-
    0x250c: ",-",   # ,-
    0x2510: "-,",   # -,
    0x2518: "-'",   # -'
    0x252c: "-T-",  # -T-
    0x2192: "->",   # -> rightwards arrow
    0x2190: "<-",   # <- leftwards arrow
    0x2194: "<->",  # <->
    0x21d2: "=>",   # =>
    0x2026: "...",  # ... ellipsis
    0x2713: "OK",   # OK
    0x2717: "NO",   # NO
    0x2139: "(i)",  # (i)
    0x25c4: "<",    # <
    0x25bc: "v",    # v
    0x2208: " in ", #  in 
    0x2265: ">=",   # >=
    0x2264: "<=",   # <=
    0x00d7: "x",    # x multiplication sign
    0x2022: "*",    # * bullet
}


def _walk_targets():
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".py", ".md", ".mjs", ".js")):
                yield os.path.join(root, f)


def _convert(text: str) -> tuple[str, int]:
    """Return (new_text, count_replaced)."""
    n = 0
    parts: list[str] = []
    for c in text:
        cp = ord(c)
        if cp <= 127:
            parts.append(c)
            continue
        repl = REPLACEMENTS.get(cp)
        if repl is None:
            parts.append(c)  # untouched -- caller should investigate
        else:
            parts.append(repl)
            n += 1
    return "".join(parts), n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Write files in place (default: dry-run summary only).")
    args = ap.parse_args()

    total_files = 0
    total_replacements = 0
    untouched: dict[str, int] = {}

    for path in _walk_targets():
        try:
            text = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        new, n = _convert(text)
        if n == 0:
            continue
        for c in new:
            if ord(c) > 127:
                untouched.setdefault(c, 0)
                untouched[c] += 1
        total_files += 1
        total_replacements += n
        rel = os.path.relpath(path).replace(os.sep, "/")
        print(f"  {rel}  -- {n} replacements")
        if args.apply:
            with open(path, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(new)

    print()
    print(f"{'Applied' if args.apply else 'Dry-run'}: {total_replacements} replacements across {total_files} files")
    if untouched:
        print("Codepoints NOT mapped (still non-ASCII after pass):")
        for c, n in sorted(untouched.items(), key=lambda x: -x[1]):
            print(f"  {hex(ord(c)):>8}  count={n}")
    if not args.apply:
        print("\nRe-run with --apply to write changes.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    main()
