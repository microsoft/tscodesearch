"""
Tests for accesses_on with C# 9 with-expression record mutation syntax.

Replicates bug discovered in Round 15 of guided testing:

  Round 15 — accesses_on missed members mutated via with-expression:

        Point moved = p with { X = 10 };
        Point both  = p with { X = 1, Y = 2 };

      `q_accesses_on` only walked `member_access_expression` and
      `conditional_access_expression` nodes. With-expressions use a distinct
      `with_expression` AST node whose members are `with_initializer` children
      (each a bare `identifier` for the property name, no field name, no
      `member_access_expression` involved).
      Fix: added a loop over `with_expression` nodes. The first child is the
      source identifier; if it is in `var_names`, each `with_initializer`
      child's first identifier is emitted as a member access.

Run (no Typesense needed):
    pytest tests/test_cs_with_expression_accesses.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_accesses_on

_SAMPLE = os.path.join(SAMPLE_ROOT1, "WithExpression.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)
_LINES  = _SRC.splitlines()


def _lns(results):
    return {ln for ln, _ in results}

def _members(results):
    return {txt.split("  ←")[0].lstrip(".") for _, txt in results}

def _line_no(fragment):
    for i, ln in enumerate(_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in fixture: {fragment!r}")


class TestWithExpressionAccesses(unittest.TestCase):
    """accesses_on must find member names mutated in with-expressions."""

    def _accesses(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_single_member_with_found(self):
        """c with { X = 0 } — X must appear in accesses_on results."""
        r = self._accesses("Coord")
        assert _line_no("c with { X = 0 }") in _lns(r), f"Single-member with line missing: {r}"

    def test_multi_member_both_found(self):
        """c with { X = 0, Y = 0 } — both X and Y must appear."""
        r = self._accesses("Coord")
        members = _members(r)
        assert "X" in members, f"'X' from multi-member with missing: {r}"
        assert "Y" in members, f"'Y' from multi-member with missing: {r}"

    def test_multi_member_same_line_both_reported(self):
        """Both X and Y on the same with-line must each appear as distinct results."""
        r = self._accesses("Coord")
        line = _line_no("X = 0, Y = 0")
        line_results = [(ln, txt) for ln, txt in r if ln == line]
        names = {txt.split("  ←")[0].lstrip(".") for _, txt in line_results}
        assert "X" in names and "Y" in names, \
            f"Both X and Y must appear on same-line with result: {line_results}"

    def test_multiline_with_each_member_own_line(self):
        """Multi-line with: each member reports on its own source line."""
        r = self._accesses("Coord")
        assert _line_no("X = 0,") in _lns(r), f"X = 0 line missing: {r}"
        assert _line_no("Y = 0,") in _lns(r), f"Y = 0 line missing: {r}"
        assert _line_no("Z = 0,") in _lns(r), f"Z = 0 line missing: {r}"

    def test_regular_access_still_found(self):
        """Regression: c.X (direct member access) must still appear."""
        r = self._accesses("Coord")
        assert _line_no("int x = c.X") in _lns(r), f"Regular access line missing: {r}"

    def test_different_type_not_in_coord_results(self):
        """Color with-expression members must NOT appear in Coord results."""
        r = self._accesses("Coord")
        lns = _lns(r)
        assert _line_no("col with { R = 0") not in lns, \
            f"Color with-expression line must not appear in Coord results: {r}"

    def test_color_with_found_when_queried(self):
        """col with { R = 0, G = 0 } must appear when querying Color."""
        r = self._accesses("Color")
        assert _line_no("col with { R = 0") in _lns(r), f"Color with-expression missing: {r}"

    def test_results_sorted_by_line(self):
        r = self._accesses("Coord")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_type_empty(self):
        assert self._accesses("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
