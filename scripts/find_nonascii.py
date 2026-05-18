"""Audit non-ASCII characters across the codebase.

Categorises every non-ASCII codepoint by location:
  * docstring     -- inside a triple-quoted block (tool descriptions live here)
  * string_literal -- non-comment line that contains a quoted string
  * comment       -- line starts with ``#``
  * code          -- other code-bearing line (rare)

Hot-path modules (those producing tool output) are listed separately so we
can focus the cleanup. Run from the repo root.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata

SKIP_DIRS = {
    ".tantivy", ".git", "node_modules", "sample", ".client-venv",
    "__pycache__", ".pytest_cache", "vscode-codesearch",
}
HOT_PREFIXES = (
    "query/", "indexserver/", "mcp_server.py", "tsquery_server.py",
)


def _rel(path: str) -> str:
    """Normalise path to forward slashes, relative to cwd."""
    rel = os.path.relpath(path, os.getcwd())
    return rel.replace(os.sep, "/")


def _is_hot(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in HOT_PREFIXES)


def _walk_py() -> list[str]:
    out = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def _classify_line(line: str, in_docstring: bool) -> str:
    stripped = line.lstrip()
    if in_docstring:
        return "docstring"
    if stripped.startswith("#"):
        return "comment"
    if '"' in line or "'" in line:
        return "string_literal"
    return "code"


def _scan(path: str):
    by_cat: dict[str, list] = {}
    in_docstring = False
    tag = None
    for ln, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
        # Crude triple-string tracking: count occurrences of triple quote in
        # the line. Two on one line = pair (in/out cancels), odd = toggle.
        for q in ('"""', "'''"):
            count = line.count(q)
            if count % 2 == 1:
                in_docstring = not in_docstring
                tag = q if in_docstring else None
        non_ascii = [c for c in line if ord(c) > 127]
        if not non_ascii:
            continue
        cat = _classify_line(line, in_docstring)
        by_cat.setdefault(cat, []).append((ln, non_ascii, line))
    return by_cat


def main() -> None:
    hot_totals: dict[str, int] = {}
    cold_totals: dict[str, int] = {}
    hot_examples: list[tuple[str, int, str, str]] = []
    for p in _walk_py():
        rel = _rel(p)
        by_cat = _scan(p)
        if not by_cat:
            continue
        target = hot_totals if _is_hot(rel) else cold_totals
        for cat, rows in by_cat.items():
            target[cat] = target.get(cat, 0) + len(rows)
            if _is_hot(rel) and cat in ("docstring", "string_literal"):
                for ln, non_ascii, line in rows[:3]:
                    hot_examples.append((rel, ln, cat, line.strip()[:110]))

    print("Hot-path (output-producing) modules:")
    for cat, n in sorted(hot_totals.items(), key=lambda x: -x[1]):
        print(f"  {cat:20s} {n}")
    print()
    print("Cold (comments / tests / scripts):")
    for cat, n in sorted(cold_totals.items(), key=lambda x: -x[1]):
        print(f"  {cat:20s} {n}")
    print()
    print("Hot-path examples (docstring or string-literal):")
    for rel, ln, cat, snippet in hot_examples[:30]:
        print(f"  {rel}:{ln}  [{cat}]  {snippet}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    main()
