"""
Tests for accesses_on with C# object initializer syntax.

Replicates bug discovered in Round 14 of guided testing:

  Round 14 — accesses_on missed members set via object initializers:

        new AbsBlobInfo { AbsInfo = absInfo }
        new Widget { Value = 5, Name = "test" }

      `q_accesses_on` only walked `member_access_expression` and
      `conditional_access_expression` nodes — both require a named variable
      on the left. Object initializer syntax uses `assignment_expression`
      nodes inside `initializer_expression` inside `object_creation_expression`,
      with a bare `identifier` on the LHS. No named variable of the type is
      needed since the type is known from the `new T { }` construct.
      Fix: added a loop over `object_creation_expression` nodes whose type
      matches; for each, walk into `initializer_expression` →
      `assignment_expression` and emit the LHS identifier as the member name.
      Members bypass the `seen_rows` dedup so that multiple members on the
      same line are all reported.

Run (no Typesense needed):
    pytest tests/test_cs_object_initializer_accesses.py -v
"""
from __future__ import annotations

import os
import unittest

from tests.base import _parse
from query.dispatch import q_accesses_on

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_SAMPLE = os.path.join(_ROOT, "sample", "root1", "ObjectInitializer.cs")

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


class TestObjectInitializerAccesses(unittest.TestCase):
    """accesses_on must find member names set in object initializers."""

    def _accesses(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_single_member_initializer_found(self):
        """new Widget { Value = 42 } — Value must appear in accesses_on results."""
        r = self._accesses("Widget")
        assert _line_no("Value = 42") in _lns(r), f"Single-member initializer line missing: {r}"

    def test_multi_member_both_found(self):
        """new Widget { Value = 1, Name = 'hello' } — both members must appear."""
        r = self._accesses("Widget")
        members = _members(r)
        assert "Value" in members, f"'Value' from multi-member initializer missing: {r}"
        assert "Name" in members, f"'Name' from multi-member initializer missing: {r}"

    def test_multi_member_same_line_both_reported(self):
        """Both Value and Name on the same line must each appear as distinct results."""
        r = self._accesses("Widget")
        line = _line_no("Value = 1, Name")
        line_results = [(ln, txt) for ln, txt in r if ln == line]
        member_names = {txt.split("  ←")[0].lstrip(".") for _, txt in line_results}
        assert "Value" in member_names, f"'Value' missing from same-line initializer: {r}"
        assert "Name" in member_names, f"'Name' missing from same-line initializer: {r}"

    def test_multiline_initializer_each_member_on_own_line(self):
        """Multi-line initializer: each member reports on its own source line."""
        r = self._accesses("Widget")
        assert _line_no("Value = 99") in _lns(r), f"Value = 99 line missing: {r}"
        assert _line_no("Name = \"world\"") in _lns(r), f"Name = world line missing: {r}"

    def test_regular_member_access_still_found(self):
        """Regression: w.Value (direct access) must still appear."""
        r = self._accesses("Widget")
        assert _line_no("int v = w.Value") in _lns(r), f"Regular access line missing: {r}"

    def test_other_type_not_in_widget_results(self):
        """Gadget initializer must NOT appear in Widget results."""
        r = self._accesses("Widget")
        lns = _lns(r)
        assert _line_no("Size = 3.14") not in lns, \
            f"Gadget initializer line must not appear in Widget results: {r}"

    def test_gadget_initializer_found_when_queried(self):
        """new Gadget { Size = 3.14 } must appear when querying Gadget."""
        r = self._accesses("Gadget")
        assert _line_no("Size = 3.14") in _lns(r), f"Gadget initializer missing: {r}"

    def test_results_sorted_by_line(self):
        r = self._accesses("Widget")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_type_empty(self):
        assert self._accesses("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
