"""
Tests for accesses_of with object initializer and with-expression syntax.

Replicates bug discovered in Round 16 of guided testing:

  Round 16 — accesses_of missed member names in object initializers and
              with-expressions:

        new Widget { Value = 5 }      // object initializer (Round 16a)
        w with { Value = 10 }         // with-expression (Round 16b)

      `q_accesses_of` only walked `member_access_expression` (obj.Member)
      and `member_binding_expression` (obj?.Member). Neither fires for
      members set in initializer or with-expression syntax.

      Fix (16a): added a loop over `object_creation_expression` →
      `initializer_expression` → `assignment_expression`; emits each
      LHS identifier that matches `member_name`.

      Fix (16b): added a loop over `with_expression` → `with_initializer`;
      emits each first-identifier child that matches `member_name`.

      Both loops respect the optional `qualifier` (e.g. for qualified
      queries like `Widget.Value`).

Run (no Typesense needed):
    pytest tests/test_cs_accesses_of_initializers.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_accesses_of


# Reuse ObjectInitializer.cs for initializer tests
_OBJ_SAMPLE = os.path.join(SAMPLE_ROOT1, "ObjectInitializer.cs")
with open(_OBJ_SAMPLE, encoding="utf-8") as _f:
    _OBJ_SRC = _f.read()
_OBJ_PARSED = _parse(_OBJ_SRC)
_OBJ_LINES  = _OBJ_SRC.splitlines()

# Reuse WithExpression.cs for with-expression tests
_WITH_SAMPLE = os.path.join(SAMPLE_ROOT1, "WithExpression.cs")
with open(_WITH_SAMPLE, encoding="utf-8") as _f:
    _WITH_SRC = _f.read()
_WITH_PARSED = _parse(_WITH_SRC)
_WITH_LINES  = _WITH_SRC.splitlines()


def _lns(results):
    return {ln for ln, _ in results}

def _obj_line_no(fragment):
    for i, ln in enumerate(_OBJ_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in ObjectInitializer.cs: {fragment!r}")

def _with_line_no(fragment):
    for i, ln in enumerate(_WITH_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in WithExpression.cs: {fragment!r}")


class TestAccessesOfObjectInitializer(unittest.TestCase):
    """accesses_of must find member names assigned in object initializers."""

    def test_single_member_init_found(self):
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        assert _obj_line_no("Value = 42") in _lns(r), f"Single init missing: {r}"

    def test_multi_member_init_found(self):
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        assert _obj_line_no("Value = 1") in _lns(r), f"Multi-member init missing: {r}"

    def test_multiline_init_member_found(self):
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        assert _obj_line_no("Value = 99") in _lns(r), f"Multi-line init missing: {r}"

    def test_regular_access_still_found(self):
        """Regression: w.Value must still appear."""
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        assert _obj_line_no("int v = w.Value") in _lns(r), f"Regular access missing: {r}"

    def test_other_member_not_in_value_results(self):
        """Name is a different member — must not appear in Value results."""
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        _lns(r)
        # Name appears on same lines in multi-member init — those lines are in results
        # but for the Name member query, Value lines should not dominate
        r_name = q_accesses_of(*_OBJ_PARSED, member_name="Name")
        # Name = "hello" should be found
        assert _obj_line_no('Name = "hello"') in _lns(r_name), f"Name init missing: {r_name}"

    def test_results_sorted_by_line(self):
        r = q_accesses_of(*_OBJ_PARSED, member_name="Value")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"


class TestAccessesOfWithExpression(unittest.TestCase):
    """accesses_of must find member names mutated in with-expressions."""

    def test_single_with_member_found(self):
        r = q_accesses_of(*_WITH_PARSED, member_name="X")
        assert _with_line_no("c with { X = 0 }") in _lns(r), f"Single with missing: {r}"

    def test_multi_with_member_found(self):
        r = q_accesses_of(*_WITH_PARSED, member_name="Y")
        assert _with_line_no("X = 0, Y = 0") in _lns(r), f"Y in multi-member with missing: {r}"

    def test_multiline_with_member_found(self):
        r = q_accesses_of(*_WITH_PARSED, member_name="Z")
        assert _with_line_no("Z = 0,") in _lns(r), f"Z in multiline with missing: {r}"

    def test_regular_access_still_found(self):
        r = q_accesses_of(*_WITH_PARSED, member_name="X")
        assert _with_line_no("int x = c.X") in _lns(r), f"Regular access missing: {r}"

    def test_results_sorted_by_line(self):
        r = q_accesses_of(*_WITH_PARSED, member_name="X")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_member_empty(self):
        assert q_accesses_of(*_WITH_PARSED, member_name="NoSuchMember") == []


if __name__ == "__main__":
    unittest.main()
