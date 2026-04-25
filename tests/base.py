"""
Common base helpers for codesearch tests.

  _parse(src)       Parse a C# source string; returns (bytes, tree, lines).
  LiveTestBase      Base class for integration tests that index into Typesense.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.request
import urllib.parse

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
    """Base for live Typesense integration tests.

    Subclasses must implement setUpClass/tearDownClass that populate
    cls.coll (collection name) and cls.tmpdir (indexed directory).
    """

    coll: str
    tmpdir: str

    def _ts_search(self, query: str, query_by: str, per_page: int = 10,
                   num_typos: int = 0) -> set[str]:
        """Run a Typesense search and return the set of matched filenames."""
        from indexserver.config import HOST, PORT, API_KEY
        params = urllib.parse.urlencode({
            "q":         query,
            "query_by":  query_by,
            "per_page":  per_page,
            "prefix":    "false",
            "num_typos": str(num_typos),
        })
        url = f"http://{HOST}:{PORT}/collections/{self.coll}/documents/search?{params}"
        req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
        with urllib.request.urlopen(req, timeout=10) as r:
            return {h["document"]["filename"]
                    for h in json.loads(r.read()).get("hits", [])}
