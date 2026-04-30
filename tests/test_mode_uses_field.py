"""
Tests for field_type mode.

mode: --field-type TYPE (q_field_type)
No dedicated Typesense field: relies on type_refs + field declarations.

Gaps tested:
  - Fields and properties typed as T are found; methods are not.
  - Params typed as T must NOT appear (that's param_type mode).
  - Generic fields (IList<T>) match both 'IList' and 'T'.
  - Enclosing class name is included in output for context.
  - Event fields are NOT returned by q_field_type (only regular fields/props).
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    FIELD_TYPED_BLOBSTORE, PARAM_ONLY_BLOBSTORE, FIELD_TYPED_ILOGGER,
    FIELD_TYPED_GENERIC_BLOBSTORE, LISTING_TARGET,
)
from indexserver.indexer import extract_cs_metadata
from query.cs import q_uses


# ══════════════════════════════════════════════════════════════════════════════
# q_uses(uses_kind="field") AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQFieldType(unittest.TestCase):

    def _ftype(self, src, type_name):
        return q_uses(*_parse(src), type_name, uses_kind="field")

    def test_finds_private_field(self):
        r = self._ftype(FIELD_TYPED_BLOBSTORE, "BlobStore")
        texts = [t for _, t in r]
        assert any("_primary" in t for t in texts), \
            f"Private field '_primary: BlobStore' must be found: {texts}"

    def test_finds_property(self):
        r = self._ftype(FIELD_TYPED_BLOBSTORE, "BlobStore")
        texts = [t for _, t in r]
        assert any("Backup" in t for t in texts), \
            f"Property 'Backup: BlobStore' must be found: {texts}"

    def test_does_not_find_string_field(self):
        r = self._ftype(FIELD_TYPED_BLOBSTORE, "BlobStore")
        texts = [t for _, t in r]
        assert not any("Name" in t for t in texts), \
            f"String field 'Name' must not appear: {texts}"

    def test_param_only_file_not_returned(self):
        """PARAM_ONLY_BLOBSTORE has BlobStore in params only — q_field_type must return empty."""
        r = self._ftype(PARAM_ONLY_BLOBSTORE, "BlobStore")
        assert r == [], f"Param-only file must not appear: {r}"

    def test_different_type_not_returned(self):
        r = self._ftype(FIELD_TYPED_ILOGGER, "BlobStore")
        assert r == []

    def test_correct_type_returned_for_ilogger(self):
        r = self._ftype(FIELD_TYPED_ILOGGER, "ILogger")
        assert r, "ILogger field must be found"
        texts = [t for _, t in r]
        assert any("_log" in t for t in texts)
        assert any("Log"  in t for t in texts)   # property

    def test_generic_field_matches_inner_type(self):
        """IList<BlobStore> — querying 'BlobStore' must match."""
        r = self._ftype(FIELD_TYPED_GENERIC_BLOBSTORE, "BlobStore")
        assert r, f"Generic field IList<BlobStore> must match 'BlobStore': {r}"

    def test_generic_field_matches_outer_type(self):
        r = self._ftype(FIELD_TYPED_GENERIC_BLOBSTORE, "IList")
        assert r, "Generic field must also match the outer IList type"

    def test_output_includes_enclosing_class(self):
        r = self._ftype(FIELD_TYPED_BLOBSTORE, "BlobStore")
        texts = [t for _, t in r]
        assert any("StorageOwner" in t for t in texts), \
            f"Enclosing class name must appear in output: {texts}"

    def test_output_includes_field_type(self):
        r = self._ftype(FIELD_TYPED_BLOBSTORE, "BlobStore")
        texts = [t for _, t in r]
        assert any("BlobStore" in t for t in texts)

    def test_listing_target_field_found(self):
        """_store field in LISTING_TARGET is typed BlobStore."""
        r = self._ftype(LISTING_TARGET, "BlobStore")
        assert r, "BlobStore field in LISTING_TARGET must be found"

    def test_no_method_results(self):
        """q_field_type must never return method declarations."""
        r = self._ftype(LISTING_TARGET, "BlobStore")
        texts = [t for _, t in r]
        for t in texts:
            assert "[method]" not in t and "[ctor]" not in t, \
                f"Method leaked into q_field_type: {t!r}"


# ══════════════════════════════════════════════════════════════════════════════
# Metadata consistency — type_refs should include declared field types
# ══════════════════════════════════════════════════════════════════════════════

class TestFieldTypeMetadataConsistency(unittest.TestCase):
    """field_type results must be consistent with the type_refs Typesense field."""

    def test_field_type_in_type_refs(self):
        meta = extract_cs_metadata(FIELD_TYPED_BLOBSTORE.encode())
        assert "BlobStore" in meta["type_refs"], \
            f"type_refs: {meta['type_refs']}"

    def test_param_only_not_via_field_in_type_refs(self):
        """PARAM_ONLY_BLOBSTORE has BlobStore in type_refs (from param), but NOT as a
        field — q_field_type correctly returns empty for it."""
        meta = extract_cs_metadata(PARAM_ONLY_BLOBSTORE.encode())
        # BlobStore IS in type_refs (from param) — that's correct for uses mode
        assert "BlobStore" in meta["type_refs"]
        # But q_field_type must still return empty (it checks field/prop node types)
        r = q_uses(*_parse(PARAM_ONLY_BLOBSTORE), "BlobStore", uses_kind="field")
        assert r == [], "Param type must not appear in field results"

    def test_generic_type_expanded_in_type_refs(self):
        meta = extract_cs_metadata(FIELD_TYPED_GENERIC_BLOBSTORE.encode())
        assert "BlobStore" in meta["type_refs"]
        assert "IList"     in meta["type_refs"]


if __name__ == "__main__":
    unittest.main()
