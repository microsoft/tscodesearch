"""
Tests that _EXT_TO_TS_AND_AST and _run_query dispatch are consistent.

The bug we fixed: accesses_of was added to _EXT_TO_TS_AND_AST (the routing
table) but not to the _run_query dispatch (the AST execution table), causing
a 500 error at runtime.

These tests catch that class of bug for all modes.

Run (no Typesense):
    pytest tests/unit/test_api_dispatch_tables.py -v
"""
from __future__ import annotations

import unittest

from tsquery_server import _EXT_TO_TS_AND_AST, _run_query


class TestDispatchConsistency(unittest.TestCase):
    """Every mode in _EXT_TO_TS_AND_AST must be handled by _run_query."""

    # Modes that are listing-only and have no meaningful pattern arg —
    # _run_query supports them but they're not pattern-searchable codebase modes.
    _LISTING_MODES = frozenset({"classes", "methods", "fields", "usings"})

    def test_all_routing_modes_handled_by_run_query(self):
        """Every mode in _EXT_TO_TS_AND_AST must not raise in _run_query."""
        missing = []
        for mode, (_, ast_mode) in _EXT_TO_TS_AND_AST.items():
            try:
                # Pass empty file list — just verifies dispatch doesn't raise
                _run_query(ast_mode, "Widget", files=[])
            except ValueError as e:
                if "unknown mode" in str(e):
                    missing.append((mode, ast_mode))
        assert not missing, (
            f"Modes in _EXT_TO_TS_AND_AST with no _run_query handler:\n"
            + "\n".join(f"  routing mode={m!r} → ast_mode={a!r}" for m, a in missing)
        )

    def test_accesses_of_in_routing_table(self):
        assert "accesses_of" in _EXT_TO_TS_AND_AST, \
            "accesses_of must be in _EXT_TO_TS_AND_AST"

    def test_accesses_of_ast_mode_is_accesses_of(self):
        _, ast_mode = _EXT_TO_TS_AND_AST["accesses_of"]
        assert ast_mode == "accesses_of", \
            f"Expected ast_mode='accesses_of', got {ast_mode!r}"

    def test_accesses_of_run_query_does_not_raise(self):
        """_run_query('accesses_of', ...) with empty file list must not raise."""
        result = _run_query("accesses_of", "Status", files=[])
        assert result == [], f"Expected empty list for no files, got {result}"


if __name__ == "__main__":
    unittest.main()
