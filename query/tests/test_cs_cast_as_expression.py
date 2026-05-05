"""
Tests for uses_kind=cast with 'as' expressions.

Replicates bug discovered in Round 13 of guided testing:

  Round 13 — uses_kind=cast missed 'as' cast expressions:

        Dog d = a as Dog;
        Dog dog = obj as Dog;

      `q_casts` only walked `cast_expression` nodes, which cover C-style
      explicit casts like `(Dog)a`. The `as` operator produces a separate
      `as_expression` node (fields: `left`=value, `right`=type). Neither
      the node type nor the correct field name was handled.
      Fix: added a loop over `as_expression` nodes reading the `right` field
      as the cast target type.

Run (no Typesense needed):
    pytest query/tests/test_cs_cast_as_expression.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_uses

_SAMPLE = os.path.join(SAMPLE_ROOT1, "CastExpressions.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)
_LINES  = _SRC.splitlines()


def _lns(results):
    return {ln for ln, _ in results}

def _texts(results):
    return " ".join(t for _, t in results)

def _line_no(fragment):
    for i, ln in enumerate(_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in fixture: {fragment!r}")


class TestCastAsExpression(unittest.TestCase):
    """uses_kind=cast must find both explicit (T)x casts and 'as' expressions."""

    def _casts(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="cast")

    def test_as_cast_found(self):
        """Dog d = a as Dog — must appear in cast results."""
        r = self._casts("Dog")
        assert r, "Expected Dog cast results"
        lns = _lns(r)
        assert _line_no("a as Dog") in lns, f"'a as Dog' line missing: {r}"

    def test_as_cast_nested_found(self):
        """Dog dog = obj as Dog — second as-cast must also appear."""
        r = self._casts("Dog")
        assert _line_no("obj as Dog") in _lns(r), f"'obj as Dog' line missing: {r}"

    def test_explicit_cast_still_found(self):
        """Regression: (Dog)a must still appear."""
        r = self._casts("Dog")
        assert _line_no("(Dog)a") in _lns(r), f"explicit cast line missing: {r}"

    def test_other_type_not_in_dog_results(self):
        """Cat casts must NOT appear in Dog results."""
        r = self._casts("Dog")
        lns = _lns(r)
        assert _line_no("a as Cat") not in lns, f"Cat line must not appear in Dog casts: {r}"

    def test_cat_as_cast_found_when_queried(self):
        """Cat c = a as Cat must appear when querying Cat."""
        r = self._casts("Cat")
        assert _line_no("a as Cat") in _lns(r), f"'a as Cat' missing from Cat casts: {r}"

    def test_results_sorted_by_line(self):
        r = self._casts("Dog")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_type_empty(self):
        assert self._casts("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
