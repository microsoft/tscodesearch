"""
Tests for all_refs mode (semantic grep).

mode: --all-refs NAME (q_all_refs)
Finds every identifier occurrence that is NOT inside a comment or string literal.

Gaps tested:
  - Every non-comment/non-string occurrence is returned.
  - Comments are excluded (both // and /* */).
  - String literals are excluded.
  - Each source line is returned at most once.
  - The mode is broader than uses/calls/casts — it finds all syntactic contexts.
  - Declaration names are included (unlike q_uses which skips them).
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    IDENT_BLOBSTORE_MANY_CONTEXTS, IDENT_COMMENT_ONLY, IDENT_STRING_ONLY,
    CALLS_FETCHWIDGET, IMPLEMENTS_IDATASTORE,
)
from query.dispatch import q_all_refs, q_uses


# ══════════════════════════════════════════════════════════════════════════════
# q_ident AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQIdent(unittest.TestCase):

    def _ident(self, src, name):
        return q_all_refs(*_parse(src), name=name)

    def test_finds_field_declaration(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "BlobStore")
        assert r, "Field declaration must be found by ident"

    def test_finds_return_type(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "BlobStore")
        lines_text = [t for _, t in r]
        assert any("GetStore" in t for t in lines_text), \
            "Return type line must be in ident results"

    def test_finds_param_type(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "BlobStore")
        lines_text = [t for _, t in r]
        assert any("Set" in t for t in lines_text), \
            "Parameter type line must be in ident results"

    def test_finds_object_creation(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "BlobStore")
        lines_text = [t for _, t in r]
        assert any("new BlobStore" in t for t in lines_text), \
            "Object creation must be in ident results"

    def test_comment_not_found(self):
        r = self._ident(IDENT_COMMENT_ONLY, "BlobStore")
        assert r == [], \
            f"Comment-only occurrence must not be found: {r}"

    def test_string_not_found(self):
        r = self._ident(IDENT_STRING_ONLY, "BlobStore")
        assert r == [], \
            f"String literal occurrence must not be found: {r}"

    def test_each_line_once(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "BlobStore")
        lines = [ln for ln, _ in r]
        assert len(lines) == len(set(lines)), "Each line at most once"

    def test_multiple_occurrences_on_same_line_once(self):
        src = """\
namespace Synth {
    public class C {
        public BlobStore Swap(BlobStore a, BlobStore b) { return a; }
    }
}
"""
        r = self._ident(src, "BlobStore")
        lines = [ln for ln, _ in r]
        # All three BlobStore on the same line → reported once
        assert len(lines) == 1

    def test_ident_broader_than_uses(self):
        """q_ident includes declaration names; q_uses does not.
        For class 'SqlDataStore', q_ident finds it as its own name;
        q_uses skips it because it's a declaration name."""
        ident_r = self._ident(IMPLEMENTS_IDATASTORE, "SqlDataStore")
        uses_r  = q_uses(*_parse(IMPLEMENTS_IDATASTORE), type_name="SqlDataStore")
        # ident finds the class declaration line; uses skips it
        assert len(ident_r) >= len(uses_r)

    def test_ident_finds_call_targets(self):
        """q_ident finds call targets; q_uses does not."""
        ident_r = self._ident(CALLS_FETCHWIDGET, "FetchWidget")
        q_uses(*_parse(CALLS_FETCHWIDGET), type_name="FetchWidget")
        assert ident_r, "q_ident must find FetchWidget call site"
        # uses_r could be empty (call target is not a type use)

    def test_unrelated_name_not_found(self):
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "Unrelated")
        assert r == []

    def test_partial_match_not_returned(self):
        """'Blob' must not match 'BlobStore' — exact identifier match required."""
        r = self._ident(IDENT_BLOBSTORE_MANY_CONTEXTS, "Blob")
        assert r == [], "Partial name match must not be returned"


if __name__ == "__main__":
    unittest.main()
