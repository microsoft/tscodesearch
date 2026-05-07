"""
Common base helpers for codesearch tests.

  _parse(src)       Parse a C# source string; returns (bytes, tree, lines).
  LiveTestBase      Base class for integration tests that index into Tantivy.
"""
from __future__ import annotations

import os
import sys
import unittest

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


class LiveTestBase(unittest.TestCase):
    """Base for live Tantivy integration tests.

    Subclasses must populate cls.coll (collection name) and cls.tmpdir
    (indexed directory). cls.backend is opened in setUpClass to a Tantivy
    Backend on disk.
    """

    coll: str
    tmpdir: str
    backend = None

    def _ts_search(self, query: str, query_by: str, per_page: int = 10,
                   num_typos: int = 0) -> set[str]:
        """Run a backend search and return the set of matched filenames."""
        from indexserver.search import search as _search
        from indexserver.indexer import ensure_backend
        from indexserver.config import load_config as _load_config
        backend = self.backend if self.backend is not None else ensure_backend(
            _load_config(), self.coll, write=False,
        )
        try:
            result = _search(
                backend,
                q=query,
                query_by=query_by,
                per_page=per_page,
                num_typos=num_typos,
            )
        finally:
            if self.backend is None:
                backend.close()
        return {h["document"]["filename"] for h in result.get("hits", [])}
