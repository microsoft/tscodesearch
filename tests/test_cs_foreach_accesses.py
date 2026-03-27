"""
Tests for q_accesses_on with foreach iteration variables.

Replicates behavior discovered in Round 4 of guided testing:

  accesses_on TYPE
    → was NOT tracking explicit-type foreach iteration variables:
      `foreach (Item item in items)` — 'item' was silently omitted from the
      tracked variable set because the implementation walked variable_declaration,
      parameter, and property_declaration, but NOT foreach_statement.

      foreach_statement in the tree-sitter C# grammar exposes:
        field "type"  — the declared type (identifier, or implicit_type for var)
        field "left"  — the iteration variable name
        field "right" — the collection expression
        field "body"  — the loop body

      Fix: added a foreach_statement loop in q_accesses_on, mirroring the
      parameter loop. Skips implicit_type (var) nodes since the element type
      cannot be determined without type inference.

Run (no Typesense needed):
    pytest tests/test_cs_foreach_accesses.py -v
"""
from __future__ import annotations

import os
import unittest

from tests.base import _parse
from src.query.dispatch import q_accesses_on

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_SAMPLE = os.path.join(_ROOT, "sample", "root1", "ForeachAccess.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)


def _lines(results):
    return {ln for ln, _ in results}

def _texts(results):
    return " ".join(t for _, t in results)


class TestAccessesOnForeach(unittest.TestCase):
    """
    q_accesses_on walked variable_declaration (covers local + field),
    parameter, and property_declaration — but not foreach_statement.
    A variable declared as the iteration variable of a foreach loop was
    therefore never tracked, and any member access on it was silently missed.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_explicit_foreach_variable_tracked(self):
        """foreach (Item item in items) — item.Name must be found."""
        r = self._on("Item")
        assert r, "Expected Item member accesses via foreach iteration variable"
        texts = _texts(r)
        assert "Name" in texts, f"item.Name missing from results: {r}"

    def test_explicit_foreach_count_access_found(self):
        """item.Count (second access in the loop body) must appear."""
        r = self._on("Item")
        texts = _texts(r)
        assert "Count" in texts, f"item.Count missing from results: {r}"

    def test_explicit_foreach_on_correct_lines(self):
        """
        ProcessAll body has two Item-member accesses on separate lines:
          item.Name  (Log call line)
          item.Count (Total += line)
        Both lines must be in results.
        """
        r = self._on("Item")
        src_lines = _SRC.splitlines()
        name_line = next(
            (i + 1 for i, ln in enumerate(src_lines) if "item.Name" in ln and "Log" in ln),
            None
        )
        count_line = next(
            (i + 1 for i, ln in enumerate(src_lines) if "item.Count" in ln),
            None
        )
        assert name_line is not None, "ForeachAccess.cs must have item.Name line"
        assert count_line is not None, "ForeachAccess.cs must have item.Count line"
        found = _lines(r)
        assert name_line in found, f"Line {name_line} (item.Name) missing: {r}"
        assert count_line in found, f"Line {count_line} (item.Count) missing: {r}"

    def test_nested_foreach_outer_variable_tracked(self):
        """
        ProcessNested has foreach (Item a in outer) — a.Name must be found.
        """
        r = self._on("Item")
        src_lines = _SRC.splitlines()
        nested_line = next(
            (i + 1 for i, ln in enumerate(src_lines) if "a.Name + b.Name" in ln),
            None
        )
        assert nested_line is not None, "ForeachAccess.cs must have a.Name + b.Name line"
        assert nested_line in _lines(r), \
            f"Line {nested_line} (nested foreach access) missing: {r}"

    def test_var_foreach_not_tracked(self):
        """
        foreach (var entry in items) — 'entry' type cannot be resolved
        without type inference; var-inferred iteration variables are not tracked.
        The var loop (ProcessVar) must NOT produce spurious results.
        (entry.Name must be absent since it is on a different line from explicit hits.)
        """
        r = self._on("Item")
        src_lines = _SRC.splitlines()
        var_line = next(
            (i + 1 for i, ln in enumerate(src_lines) if "entry.Name" in ln),
            None
        )
        assert var_line is not None, "ForeachAccess.cs must have entry.Name line"
        assert var_line not in _lines(r), \
            f"var-inferred foreach line {var_line} must not appear in Item accesses: {r}"

    def test_unrelated_type_returns_empty(self):
        assert self._on("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
