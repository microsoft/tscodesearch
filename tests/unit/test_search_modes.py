"""
Unit tests for ``indexserver.search_modes.resolve_query_params``.

Verifies the mode -> ``(query_by, weights)`` mapping the daemon and the
standalone search CLI share.
"""
from __future__ import annotations

import unittest

from indexserver.search_modes import resolve_query_params


class TestResolveQueryParams(unittest.TestCase):

    def test_calls_includes_qualified_calls(self):
        """``calls`` mode must query both bare and ``Type.Method`` fields.

        Allows the agent to pass either ``Save`` (bare) or
        ``IRepository.Save`` (resolved) without picking the right field.
        """
        query_by, weights = resolve_query_params("calls", "", "")
        fields = query_by.split(",")
        assert "call_sites" in fields
        assert "qualified_calls" in fields
        # Weights must be parallel to fields (same length, all numeric).
        weight_parts = weights.split(",")
        assert len(weight_parts) == len(fields), (query_by, weights)
        for w in weight_parts:
            float(w)  # raises if non-numeric

    def test_implements_unchanged(self):
        query_by, _ = resolve_query_params("implements", "", "")
        assert "base_types" in query_by
        assert "class_names" in query_by

    def test_uses_field_kind_unchanged(self):
        query_by, _ = resolve_query_params("uses", "field", "")
        assert "field_types" in query_by

    def test_unknown_mode_falls_back(self):
        query_by, _ = resolve_query_params("totally-unknown", "", "")
        # Falls back to the broad all_refs mapping.
        assert "tokens" in query_by


if __name__ == "__main__":
    unittest.main()
