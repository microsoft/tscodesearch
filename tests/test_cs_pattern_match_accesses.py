"""
Tests for q_accesses_on with C# pattern-matching variable bindings.

Replicates behavior discovered in Round 5 of guided testing:

  accesses_on TYPE
    → was NOT tracking variables bound by is-pattern expressions or
      switch-case declaration patterns:

        if (child is Widget w) { w.Name ... }     // is_pattern_expression
        case Widget w: w.Name ...                  // switch_section / declaration_pattern

      Both forms produce a `declaration_pattern` AST node:
        field "type" — the matched type
        field "name" — the bound variable identifier

      Fix: added a declaration_pattern loop in q_accesses_on, mirroring the
      foreach_statement loop added in Round 4.

Run (no Typesense needed):
    pytest tests/test_cs_pattern_match_accesses.py -v
"""
from __future__ import annotations

import os
import unittest

from tests.base import _parse
from src.query.dispatch import q_accesses_on

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_SAMPLE = os.path.join(_ROOT, "sample", "root1", "PatternMatch.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)
_LINES  = _SRC.splitlines()


def _lines_found(results):
    return {ln for ln, _ in results}

def _texts(results):
    return " ".join(t for _, t in results)

def _line_no(fragment):
    """Return 1-based line number of the first line containing fragment."""
    for i, ln in enumerate(_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in fixture: {fragment!r}")


class TestIsPatternBinding(unittest.TestCase):
    """
    `if (s is Circle c)` — 'c' was silently omitted from the tracked variable
    set even though its type is explicitly stated in the is-pattern.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_if_is_pattern_radius_found(self):
        """if (s is Circle c) { Render(c.Radius) } — c.Radius must appear."""
        r = self._on("Circle")
        assert "Radius" in _texts(r), f"c.Radius missing: {r}"

    def test_if_is_pattern_color_found(self):
        """if (s is Circle c) { Log(c.Color) } — c.Color must appear."""
        r = self._on("Circle")
        assert "Color" in _texts(r), f"c.Color missing: {r}"

    def test_if_is_pattern_on_correct_lines(self):
        r = self._on("Circle")
        found = _lines_found(r)
        assert _line_no("Render(c.Radius)") in found
        assert _line_no("Log(c.Color)") in found

    def test_unrelated_type_returns_empty(self):
        assert self._on("NoSuchType") == []


class TestSwitchCasePattern(unittest.TestCase):
    """
    `case Rectangle r:` — 'r' typed as Rectangle via a switch-case pattern.
    Same `declaration_pattern` AST node as if-is, different syntactic context.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_switch_case_width_found(self):
        r = self._on("Rectangle")
        assert "Width" in _texts(r), f"r.Width missing: {r}"

    def test_switch_case_height_found(self):
        r = self._on("Rectangle")
        assert "Height" in _texts(r), f"r.Height missing: {r}"

    def test_switch_case_on_correct_line(self):
        r = self._on("Rectangle")
        assert _line_no("r.Width * r.Height") in _lines_found(r)

    def test_multiple_switch_cases_same_type(self):
        """Both `case Circle ci:` arms must contribute to tracked var names."""
        r = self._on("Circle")
        # The ci.Radius line inside the switch must appear
        assert _line_no("ci.Radius * ci.Radius") in _lines_found(r)


class TestCombinedCondition(unittest.TestCase):
    """
    `if (obj is Circle combo && combo.Radius > 0)` — the binding and its first
    use are on the same line; a second use is on the next line.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_inline_use_in_condition_found(self):
        r = self._on("Circle")
        assert _line_no("combo.Radius > 0") in _lines_found(r)

    def test_body_use_after_pattern_found(self):
        r = self._on("Circle")
        assert _line_no("Log(combo.Color)") in _lines_found(r)


class TestPlainLocalStillWorks(unittest.TestCase):
    """Regression: plain typed local variable must still be tracked."""

    def test_plain_local_color_found(self):
        r = q_accesses_on(*_PARSED, type_name="Circle")
        assert _line_no("Log(local.Color)") in _lines_found(r)

    def _lines_found(self, results):
        return {ln for ln, _ in results}


if __name__ == "__main__":
    unittest.main()
