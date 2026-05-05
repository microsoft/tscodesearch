"""
Tests for recursive_pattern support in uses_kind=locals and accesses_on.

Round 17 — uses_kind=locals and accesses_on missed bindings from property
            pattern matching (recursive_pattern):

      if (obj is Widget { Size: 0 } wp)   // recursive_pattern
      case Widget { Size: > 0 } ws:        // recursive_pattern in switch arm

    `_iter_all_locals` and `_collect_typed_var_names` only listed
    `declaration_pattern` in their node-type sets; `recursive_pattern` was
    silently skipped even though it exposes identical `type` and `name` fields.

    Fix: added `recursive_pattern` to the node-type sets in both
    `_iter_single_field_locals` calls (one in `_iter_all_locals`, one in
    `_collect_typed_var_names`).

Run (no Typesense needed):
    pytest query/tests/test_cs_recursive_pattern.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_uses, q_accesses_on

_SAMPLE = os.path.join(SAMPLE_ROOT1, "RecursivePattern.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()
_PARSED = _parse(_SRC)
_LINES  = _SRC.splitlines()


def _lns(results):
    return {ln for ln, _ in results}


def _line_no(fragment):
    for i, ln in enumerate(_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in RecursivePattern.cs: {fragment!r}")


class TestRecursivePatternLocals(unittest.TestCase):
    """uses_kind=locals must find bindings in recursive (property) patterns."""

    def test_prop_pattern_binding_found(self):
        """if (obj is Widget { Size: 0 } wp) — wp must be found."""
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        assert _line_no("obj is Widget { Size: 0 } wp") in _lns(r), (
            f"Property pattern binding missing: {r}"
        )

    def test_switch_prop_pattern_binding_found(self):
        """case Widget { Size: > 0 } ws: — ws must be found."""
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        assert _line_no("case Widget { Size: > 0 } ws") in _lns(r), (
            f"Switch prop-pattern binding missing: {r}"
        )

    def test_plain_pattern_regression(self):
        """if (obj is Widget w) — plain declaration_pattern must still be found."""
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        assert _line_no("if (obj is Widget w)") in _lns(r), (
            f"Plain declaration_pattern regression: {r}"
        )

    def test_no_binding_not_found(self):
        """if (obj is Widget { Size: 0 }) with no binding name must NOT appear."""
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        # The NoBinding method has no binding var, so its source line must not appear
        no_binding_line = _line_no("if (obj is Widget { Size: 0 }) { }")
        assert no_binding_line not in _lns(r), (
            f"No-binding pattern incorrectly found: {r}"
        )

    def test_negative_other_type_not_in_widget_results(self):
        """Bindings of type Other must not appear in Widget results."""
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        r_other = q_uses(*_PARSED, type_name="Other", uses_kind="locals")
        # Other has its own binding op — must appear in Other results, not Widget results
        other_line = _line_no("obj is Other { Size: 1 } op")
        assert other_line not in _lns(r), (
            f"Other-typed binding leaked into Widget results: {r}"
        )
        assert other_line in _lns(r_other), (
            f"Other-typed binding not in Other results: {r_other}"
        )

    def test_results_sorted_by_line(self):
        r = q_uses(*_PARSED, type_name="Widget", uses_kind="locals")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"


class TestRecursivePatternAccessesOn(unittest.TestCase):
    """accesses_on must track member accesses through recursive-pattern bindings."""

    def test_prop_pattern_access_found(self):
        """wp.Use() must be found when wp is bound via recursive pattern."""
        r = q_accesses_on(*_PARSED, type_name="Widget")
        assert _line_no("wp.Use()") in _lns(r), (
            f"Access via prop-pattern binding missing: {r}"
        )

    def test_switch_prop_pattern_access_found(self):
        """ws.Use() must be found when ws is bound via switch recursive pattern."""
        r = q_accesses_on(*_PARSED, type_name="Widget")
        assert _line_no("ws.Use()") in _lns(r), (
            f"Access via switch prop-pattern binding missing: {r}"
        )

    def test_plain_pattern_access_regression(self):
        """w.Use() from plain declaration_pattern must still be found."""
        r = q_accesses_on(*_PARSED, type_name="Widget")
        assert _line_no("w.Use()") in _lns(r), (
            f"Plain pattern access regression: {r}"
        )

    def test_results_sorted_by_line(self):
        r = q_accesses_on(*_PARSED, type_name="Widget")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"


if __name__ == "__main__":
    unittest.main()
