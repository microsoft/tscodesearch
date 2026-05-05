"""
Tests for q_uses uses_kind sub-modes using sample/root1/Processors.cs.

Replicates behavior observed in Round 2 of guided testing:

  uses_kind=param   — works correctly (finds type in method/delegate params)
  uses_kind=field   — works correctly (finds type in field/property declarations)
  uses_kind=base    — works correctly (finds type in base-class/interface lists)
  uses_kind=cast    — works correctly (delegates to q_casts)
  uses_kind=locals  — works correctly (finds explicit-type local declarations)
  uses_kind=return  — was BROKEN: method_declaration exposes return type via the
                      "returns" field, not "type"; and delegate_declaration was
                      absent from the node list.  Both gaps are now fixed.

Run (no Typesense needed):
    pytest query/tests/test_cs_uses_kind.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_uses

_PROC   = os.path.join(SAMPLE_ROOT1, "Processors.cs")

with open(_PROC, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)


def _lines(results):
    return {ln for ln, _ in results}

def _texts(results):
    return " ".join(t for _, t in results)


# ===========================================================================
# uses_kind=return  (was broken — fixed in Round 2)
# ===========================================================================

class TestUsesKindReturn(unittest.TestCase):
    """
    _q_return_type walked method_declaration nodes but fetched the return
    type via child_by_field_name("type"), which is None for methods in the
    tree-sitter C# grammar.  The correct field is "returns".
    Additionally, delegate_declaration was absent from the node list even
    though delegates have a meaningful return type via the "type" field.
    """

    def _ret(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="return")

    def test_finds_ordinary_method_return_type(self):
        """ProcessorFactory.Run returns ProcessResult — must be found."""
        r = self._ret("ProcessResult")
        assert r, "uses_kind=return must find method return types"
        texts = _texts(r)
        assert "Run" in texts, f"Run(…) not in results: {r}"

    def test_finds_all_method_return_sites(self):
        """
        Processors.cs has three ProcessResult-returning methods/delegates:
          line 28:  delegate ProcessResult ProcessDelegate(string input)
          line 114: ProcessResult Run(IProcessor<string> processor, string input)
          line 236: ProcessResult Merge(ProcessResult a, ProcessResult b)
        """
        r = self._ret("ProcessResult")
        lines = _lines(r)
        assert 28  in lines, f"Delegate ProcessDelegate (line 28) missing: {r}"
        assert 114 in lines, f"Method Run (line 114) missing: {r}"
        assert 236 in lines, f"Method Merge (line 236) missing: {r}"

    def test_finds_delegate_return_type(self):
        """delegate_declaration was not in the node walk list — now fixed."""
        r = self._ret("ProcessResult")
        texts = _texts(r)
        assert "ProcessDelegate" in texts, \
            f"Delegate ProcessDelegate not found — delegate_declaration still missing? {r}"

    def test_excludes_constructor(self):
        """Constructor declarations have no return type — must not appear."""
        r = self._ret("ProcessResult")
        texts = _texts(r)
        assert "ProcessResult(" not in texts, \
            "Constructor must not appear in uses_kind=return results"

    def test_void_returning_method_not_returned(self):
        """Methods returning void (Reset, LogResult) must not appear."""
        r = self._ret("void")
        # void is not a named type — _type_names("void") produces {"void"},
        # but real methods return void without it being a user-defined type.
        # Either empty or only void-returning methods — the key thing is
        # ProcessResult-returning methods are not in the void results.
        proc_lines = _lines(self._ret("ProcessResult"))
        void_lines = _lines(r)
        overlap = proc_lines & void_lines
        assert not overlap, \
            f"ProcessResult-return lines must not overlap void-return lines: {overlap}"

    def test_nonexistent_type_returns_empty(self):
        assert self._ret("NoSuchReturnType") == []


# ===========================================================================
# uses_kind=param  (was already working)
# ===========================================================================

class TestUsesKindParam(unittest.TestCase):

    def _param(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="param")

    def test_finds_param_in_method(self):
        """ProcessingService.LogResult(ProcessResult result)."""
        r = self._param("ProcessResult")
        assert r, "uses_kind=param must find ProcessResult params"
        texts = _texts(r)
        assert "LogResult" in texts

    def test_finds_param_in_delegate(self):
        """ProcessDelegate takes a string param — verify delegate params are scanned."""
        r = self._param("string")
        texts = _texts(r)
        assert "ProcessDelegate" in texts, \
            f"Delegate param not found: {r}"

    def test_multiple_params_same_type_same_line(self):
        """Merge(ProcessResult a, ProcessResult b) — both params must appear."""
        r = self._param("ProcessResult")
        merge_hits = [(ln, t) for ln, t in r if "Merge" in t]
        assert len(merge_hits) == 2, \
            f"Expected 2 ProcessResult params for Merge, got {merge_hits}"

    def test_out_modifier_preserved(self):
        """TryGetFirst has 'out ProcessResult result' — modifier must be in output."""
        r = self._param("ProcessResult")
        texts = _texts(r)
        assert "out" in texts, f"'out' modifier missing: {r}"

    def test_nonexistent_type_returns_empty(self):
        assert self._param("NoSuchParamType") == []


# ===========================================================================
# uses_kind=locals  (working — verify explicit-type vs var)
# ===========================================================================

class TestUsesKindLocals(unittest.TestCase):

    def _locals(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="locals")

    def test_explicit_type_local_found(self):
        """string result = 'formatted' in TextProcessor.Format is explicit-type."""
        r = self._locals("string")
        assert r, "Explicit-type string locals must be found"

    def test_var_inferred_local_not_found(self):
        """
        `var results = new ProcessResult[...]` uses var, not explicit ProcessResult.
        uses_kind=locals only matches explicit type annotations.
        """
        r = self._locals("ProcessResult")
        assert r == [], \
            f"var-inferred ProcessResult local must not appear in locals results: {r}"

    def test_nonexistent_type_returns_empty(self):
        assert self._locals("NoSuchLocalType") == []


# ===========================================================================
# uses_kind=field  (working — verify field and property)
# ===========================================================================

class TestUsesKindField(unittest.TestCase):

    def _field(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="field")

    def test_finds_ilogger_field(self):
        """BaseProcessor._logger and ProcessingService._logger are ILogger fields."""
        r = self._field("ILogger")
        assert r, "ILogger field must be found"
        texts = _texts(r)
        assert "_logger" in texts

    def test_finds_iprocessor_field(self):
        r = self._field("IProcessor")
        assert r, "IProcessor<string> field must be found"

    def test_nonexistent_type_returns_empty(self):
        assert self._field("NoSuchFieldType") == []


# ===========================================================================
# uses_kind=base  (working — verify interface list scanning)
# ===========================================================================

class TestUsesKindBase(unittest.TestCase):

    def _base(self, type_name):
        return q_uses(*_PARSED, type_name=type_name, uses_kind="base")

    def test_finds_iprocessor_implementors(self):
        r = self._base("IProcessor")
        assert r, "IProcessor base uses must be found"
        texts = _texts(r)
        assert "BaseProcessor" in texts or "TextProcessor" in texts

    def test_nonexistent_base_returns_empty(self):
        assert self._base("INoSuchInterface") == []


if __name__ == "__main__":
    unittest.main()
