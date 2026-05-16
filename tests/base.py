"""
Common base helpers for codesearch tests.

  _parse(src)       Parse a C# source string; returns (bytes, tree, lines).
"""
from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

_CS = Language(tscsharp.language())
_parser = Parser(_CS)


def _parse(src: str):
    """Return (src_bytes, tree, lines) for a C# source string."""
    b = src.encode()
    tree = _parser.parse(b)
    lines = src.splitlines()
    return b, tree, lines
