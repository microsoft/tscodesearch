"""
Tests for null-conditional member access (?.) in q_calls, q_accesses_on, q_accesses_of.

Replicates behavior discovered in Round 7 of guided testing:

  q_calls METHOD
    → missed calls via `?.`: `obj?.Method(args)` uses a
      `conditional_access_expression` as the invocation function node, not a
      plain `member_access_expression`. The method name is in a nested
      `member_binding_expression` (field "name").

  accesses_on TYPE
    → missed `var?.Member` accesses: the result-collection loop only walked
      `member_access_expression`; `conditional_access_expression` nodes
      (where the condition is the tracked variable) were not walked.

  accesses_of MEMBER
    → same gap: `member_binding_expression` nodes (inside conditional_access)
      were not walked by q_accesses_of.

  All three bugs fixed together since they share the same root cause.
  Results for q_accesses_on and q_accesses_of are now sorted by line number.

Run (no Typesense needed):
    pytest tests/test_cs_null_conditional.py -v
"""
from __future__ import annotations

import os
import unittest

from tests.base import _parse
from query.dispatch import q_calls, q_accesses_on, q_accesses_of

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_SAMPLE = os.path.join(_ROOT, "sample", "root1", "NullConditional.cs")

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


class TestCallsNullConditional(unittest.TestCase):
    """q_calls misses `obj?.Method()` — conditional_access_expression not handled."""

    def test_null_conditional_call_found(self):
        r = q_calls(*_PARSED, method_name="LogInfo")
        assert r, "Expected LogInfo call results"
        assert _line_no("r.Log?.LogInfo") in _lns(r), f"?.LogInfo line missing: {r}"

    def test_null_conditional_error_call_found(self):
        r = q_calls(*_PARSED, method_name="LogError")
        assert _line_no("r.Log?.LogError") in _lns(r), f"?.LogError line missing: {r}"

    def test_regular_call_still_works(self):
        """Regression: non-null-conditional calls must not be broken."""
        # Logger.LogInfo declared in fixture, called via ?.
        r = q_calls(*_PARSED, method_name="LogInfo")
        assert r, "Expected at least one LogInfo result"


class TestAccessesOnNullConditional(unittest.TestCase):
    """accesses_on misses `var?.Member` — conditional_access_expression not walked."""

    def test_null_conditional_member_access_found(self):
        r = q_accesses_on(*_PARSED, type_name="Result")
        assert r, "Expected Result member accesses"
        assert _line_no("return r?.Message") in _lns(r), f"r?.Message line missing: {r}"

    def test_null_conditional_code_access_found(self):
        r = q_accesses_on(*_PARSED, type_name="Result")
        assert _line_no("return r?.Code") in _lns(r), f"r?.Code line missing: {r}"

    def test_regular_access_still_found(self):
        r = q_accesses_on(*_PARSED, type_name="Result")
        assert _line_no("return r.Message") in _lns(r), f"r.Message line missing: {r}"

    def test_results_sorted_by_line(self):
        r = q_accesses_on(*_PARSED, type_name="Result")
        lines = [ln for ln, _ in r]
        assert lines == sorted(lines), f"Results not sorted: {lines}"


class TestAccessesOfNullConditional(unittest.TestCase):
    """accesses_of misses `?.Member` — member_binding_expression not walked."""

    def test_null_conditional_access_of_member_found(self):
        r = q_accesses_of(*_PARSED, member_name="Message")
        assert r, "Expected Message accesses"
        assert _line_no("primary?.Message") in _lns(r), f"?.Message line missing: {r}"

    def test_regular_access_of_member_found(self):
        r = q_accesses_of(*_PARSED, member_name="Message")
        assert _line_no("secondary.Message") in _lns(r), f"secondary.Message missing: {r}"

    def test_accesses_of_sorted_by_line(self):
        r = q_accesses_of(*_PARSED, member_name="Message")
        lines = [ln for ln, _ in r]
        assert lines == sorted(lines), f"Results not sorted: {lines}"

    def test_unrelated_member_empty(self):
        assert q_accesses_of(*_PARSED, member_name="NoSuchMember") == []


if __name__ == "__main__":
    unittest.main()
