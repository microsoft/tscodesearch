"""
Tests for uses_kind=locals with C# pattern-matching variable bindings.

Replicates bug discovered in Round 12 of guided testing:

  Round 12 — uses_kind=locals missed declaration_pattern variables:

        if (s is Circle c)        // is-pattern binding
        case Circle ci:           // switch-case pattern binding
        if (obj is Circle combo && combo.Radius > 0)  // combined condition

      All three produce `declaration_pattern` nodes (fields: `type`, `name`)
      that appear inside `is_pattern_expression` or `case_pattern_switch_label`
      — not inside any `local_declaration_statement`.
      Fix: added a loop over all `declaration_pattern` nodes in `_q_local_type`,
      mirroring the same node type already handled in `_add_typed_vars` for
      `accesses_on` (Round 5/6).

Run (no Typesense needed):
    pytest query/tests/test_cs_pattern_locals.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_uses

_SAMPLE = os.path.join(SAMPLE_ROOT1, "PatternMatch.cs")

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


class TestPatternMatchLocals(unittest.TestCase):
    """uses_kind=locals must find variables bound by is-pattern and switch-case pattern."""

    def _locals(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="locals")

    def test_is_pattern_variable_found(self):
        """if (s is Circle c) — 'c' must appear in locals results."""
        r = self._locals("Circle")
        assert "c" in _texts(r), f"'c' from is-pattern missing: {r}"

    def test_is_pattern_on_correct_line(self):
        r = self._locals("Circle")
        assert _line_no("if (s is Circle c)") in _lns(r)

    def test_switch_case_pattern_variable_found(self):
        """case Circle ci: — 'ci' must appear in locals results."""
        r = self._locals("Circle")
        assert "ci" in _texts(r), f"'ci' from switch-case pattern missing: {r}"

    def test_switch_case_pattern_on_correct_line(self):
        r = self._locals("Circle")
        assert _line_no("case Circle ci:") in _lns(r)

    def test_combined_condition_variable_found(self):
        """if (obj is Circle combo && ...) — 'combo' must appear in locals results."""
        r = self._locals("Circle")
        assert "combo" in _texts(r), f"'combo' from combined condition missing: {r}"

    def test_combined_condition_on_correct_line(self):
        r = self._locals("Circle")
        assert _line_no("if (obj is Circle combo") in _lns(r)

    def test_plain_local_still_found(self):
        """Regression: plain typed local must still appear."""
        r = self._locals("Circle")
        assert "local" in _texts(r), f"plain local missing: {r}"

    def test_rectangle_not_in_circle_results(self):
        """Rectangle pattern variable (r) must NOT appear in Circle locals."""
        r = self._locals("Circle")
        lns = _lns(r)
        assert _line_no("case Rectangle r:") not in lns, \
            f"Rectangle pattern line must not appear in Circle results: {r}"

    def test_results_sorted_by_line(self):
        r = self._locals("Circle")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_type_empty(self):
        assert self._locals("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
