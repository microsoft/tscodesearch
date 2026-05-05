"""
Tests for uses_kind=locals with various local variable declaration forms.

Replicates bugs discovered in Rounds 8 and 9 of guided testing:

  Round 8 — uses_kind=locals missed using-statement and for-statement variables:

        using (MemoryStream stm = new MemoryStream(...))
        for (Connection cur = arr[0]; ...)

      `_q_local_type` only walked `local_declaration_statement` nodes.
      Fix: extended the walker to also cover `using_statement` and `for_statement`:
        lambda n: n.type in ("local_declaration_statement", "using_statement",
                             "for_statement")

  Round 11 — uses_kind=locals missed declaration_expression variables:

        if (TryParse(input, out Connection opened))   // out variable
        (Connection first, Connection second) = MakePair()  // tuple decon

      Both forms produce `declaration_expression` nodes (field layout: `type`,
      `name`) that appear directly inside `argument` or `tuple_expression`
      nodes — not inside a `local_declaration_statement`.
      Fix: added a loop over all `declaration_expression` nodes, skipping
      `implicit_type` (var-inferred), mirroring the Round 6 `accesses_on` fix.

  Round 10 — uses_kind=locals missed catch-clause variables:

        catch (Connection ex) { ... }

      `catch_declaration` is a direct child of `catch_clause` and has no
      `variable_declaration` child. The type and variable are plain unnamed
      `identifier` children (first = type, second = variable name).
      Fix: added a loop over `catch_clause` nodes that reads the two
      `identifier` children from the `catch_declaration`.

  Round 9 — uses_kind=locals missed foreach-statement iteration variables:

        foreach (Connection item in arr)

      `foreach_statement` uses `"type"` and `"left"` fields directly (no
      `variable_declaration` child), so the existing `var_decl` lookup
      always returned None and silently skipped it.
      Fix: added a separate loop over `foreach_statement` nodes, mirroring
      the same pattern already used in `q_accesses_on` (Round 4).

Run (no Typesense needed):
    pytest query/tests/test_cs_using_statement_locals.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_uses

_SAMPLE = os.path.join(SAMPLE_ROOT1, "UsingStatement.cs")

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


class TestUsingStatementLocals(unittest.TestCase):
    """
    uses_kind=locals missed variables declared in `using (Type var = ...)` —
    the variable_declaration is a direct child of using_statement, not inside
    a local_declaration_statement.
    """

    def _locals(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="locals")

    def test_using_statement_variable_found(self):
        r = self._locals("Connection")
        assert r, "Expected Connection locals"
        assert "conn" in _texts(r), f"'conn' from using-statement missing: {r}"

    def test_using_statement_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("using (Connection conn") in _lns(r)

    def test_nested_using_statements_both_found(self):
        r = self._locals("Connection")
        assert "c1" in _texts(r), f"'c1' missing: {r}"
        assert "c2" in _texts(r), f"'c2' missing: {r}"

    def test_plain_local_still_found(self):
        """Regression: plain typed local must still appear."""
        r = self._locals("Connection")
        assert "plain" in _texts(r), f"plain local missing: {r}"

    def test_different_type_not_in_results(self):
        """using (Transaction tx = ...) must not appear in Connection results."""
        r = self._locals("Connection")
        assert "tx" not in _texts(r), f"tx from Transaction must not appear: {r}"

    def test_using_type_found_when_queried_directly(self):
        """Transaction tx from using-statement appears in Transaction locals."""
        r = self._locals("Transaction")
        assert "tx" in _texts(r), f"'tx' missing from Transaction locals: {r}"

    def test_for_statement_variable_found(self):
        """for (Connection cur = ...) variable must appear in locals results."""
        r = self._locals("Connection")
        assert "cur" in _texts(r), f"'cur' from for-statement missing: {r}"

    def test_for_statement_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("for (Connection cur") in _lns(r)

    def test_foreach_statement_variable_found(self):
        """foreach (Connection item in arr) must appear in locals results."""
        r = self._locals("Connection")
        assert "item" in _texts(r), f"'item' from foreach-statement missing: {r}"

    def test_foreach_statement_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("foreach (Connection item") in _lns(r)

    def test_foreach_var_inferred_not_tracked(self):
        """foreach (var item in arr) must NOT appear — type is implicit."""
        r = self._locals("Connection")
        # There's only one 'item' in the fixture (the explicit-type foreach),
        # so we check the var-inferred foreach line is absent, not the name.
        lines = _lns(r)
        assert _line_no("foreach (var item") not in lines, \
            f"var-inferred foreach line must not appear: {r}"

    def test_out_var_explicit_type_found(self):
        """if (TryOpen(out Connection opened)) — out variable must appear."""
        r = self._locals("Connection")
        assert "opened" in _texts(r), f"'opened' from out-var missing: {r}"

    def test_out_var_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("out Connection opened") in _lns(r)

    def test_tuple_decon_both_vars_found(self):
        """(Connection first, Connection second) = MakePair() — both must appear."""
        r = self._locals("Connection")
        assert "first" in _texts(r), f"'first' from tuple decon missing: {r}"
        assert "second" in _texts(r), f"'second' from tuple decon missing: {r}"

    def test_tuple_decon_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("(Connection first, Connection second)") in _lns(r)

    def test_catch_clause_variable_found(self):
        """catch (Connection ex) must appear in locals results."""
        r = self._locals("Connection")
        assert "ex" in _texts(r), f"'ex' from catch-clause missing: {r}"

    def test_catch_clause_on_correct_line(self):
        r = self._locals("Connection")
        assert _line_no("catch (Connection ex)") in _lns(r)

    def test_catch_no_var_not_tracked(self):
        """catch (Transaction) with no variable must NOT produce a result."""
        r = self._locals("Transaction")
        # Transaction appears in OtherType() and CatchNoVar(); only the using()
        # version (with variable tx) should be in results — no bare catch entry
        texts = _texts(r)
        # confirm tx is found (from using-statement)
        assert "tx" in texts, f"'tx' from using-statement missing: {r}"
        # confirm no unnamed catch entry added spuriously
        catch_no_var_line = _line_no("catch (Transaction)")
        assert catch_no_var_line not in _lns(r), \
            f"bare catch(Transaction) line must not appear: {r}"

    def test_results_sorted_by_line(self):
        r = self._locals("Connection")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), f"Results not sorted: {lns}"

    def test_unrelated_type_empty(self):
        assert self._locals("NoSuchType") == []


if __name__ == "__main__":
    unittest.main()
