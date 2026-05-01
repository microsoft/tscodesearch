"""
Tests for q_accesses_on with inline out variable declarations.

Replicates behavior discovered in Round 6 of guided testing:

  accesses_on TYPE
    → was NOT tracking variables declared inline with `out`:

        if (dict.TryGetValue(key, out Token tok))
            tok.Value ...

      The `out Token tok` argument produces a `declaration_expression` AST node:
        field "type" — the declared type (identifier)
        field "name" — the variable name

      This is the same field layout as `declaration_pattern` (Round 5), so the
      fix extended the existing declaration_pattern loop to also cover
      declaration_expression.

      `out var entry` (var-inferred) is NOT tracked — the type is `implicit_type`
      and cannot be resolved without type inference.

Run (no Typesense needed):
    pytest tests/test_cs_out_var_accesses.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_accesses_on

_SAMPLE = os.path.join(SAMPLE_ROOT1, "OutVar.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)
_LINES  = _SRC.splitlines()


def _lines_found(results):
    return {ln for ln, _ in results}

def _texts(results):
    return " ".join(t for _, t in results)

def _line_no(fragment):
    for i, ln in enumerate(_LINES):
        if fragment in ln:
            return i + 1
    raise AssertionError(f"Fragment not found in fixture: {fragment!r}")


class TestInlineOutVar(unittest.TestCase):
    """
    `if (TryParse(input, out Token tok))` — tok was silently omitted from
    the tracked variable set because declaration_expression nodes were not walked.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    def test_out_typed_variable_tracked(self):
        """out Token tok — tok.Value must appear in results."""
        r = self._on("Token")
        assert r, "Expected Token member accesses"
        assert "Value" in _texts(r), f"tok.Value missing: {r}"

    def test_out_typed_variable_on_correct_line(self):
        r = self._on("Token")
        assert _line_no("tok.Value") in _lines_found(r)

    def test_out_var_inferred_not_tracked_spuriously(self):
        """
        out var entry — entry is var-inferred; its type cannot be determined
        without inference, so no accesses on it should appear.
        """
        r = self._on("Token")
        # The 'entry.Value' line is inside ParseVar which uses out var
        entry_line = _line_no("entry.Value")
        assert entry_line not in _lines_found(r), \
            f"var-inferred out entry line {entry_line} must not appear: {r}"

    def test_plain_local_still_tracked(self):
        """Regression: Token t = new Token() must still produce t.Value."""
        r = self._on("Token")
        assert _line_no("t.Value") in _lines_found(r), f"plain local missing: {r}"

    def test_unrelated_type_empty(self):
        assert self._on("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
