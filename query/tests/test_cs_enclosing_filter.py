"""
Tests for the ``enclosing_method=`` and ``enclosing_class=`` filters on
pattern modes.

The filters narrow per-hit results to those inside a member / type with
the given name. Cross-cuts every pattern mode that emits from inside
method bodies: ``calls``, ``uses``, ``casts``, ``accesses_of``,
``accesses_on``, ``all_refs``.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import (
    q_calls, q_casts, q_accesses_of, q_accesses_on, q_all_refs, q_uses,
)

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _parse(src: str):
    b = src.encode()
    tree = _PARSER.parse(b)
    return b, tree, src.splitlines()


_SRC = """\
namespace Acme {
    public class WriteBack {
        public void Run() {
            Save();
            Save();
        }
        public void Other() {
            Save();
            Save();
            Save();
        }
    }
    public class ReadOnly {
        public void Run() {
            Save();
        }
    }
}
"""


class TestEnclosingMethodFilter(unittest.TestCase):
    def test_no_filter_returns_all_hits(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Save")
        assert len(r) == 6, r

    def test_enclosing_method_narrows(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Save", enclosing_method="Run")
        # Both Run methods qualify (WriteBack.Run has 2 calls, ReadOnly.Run has 1).
        assert len(r) == 3, r
        for _, t in r:
            assert "[in WriteBack.Run]" in t or "[in ReadOnly.Run]" in t, t

    def test_enclosing_class_narrows(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Save", enclosing_class="WriteBack")
        # All 5 Save() calls in WriteBack (2 in Run + 3 in Other).
        assert len(r) == 5, r
        for _, t in r:
            assert "[in WriteBack." in t, t

    def test_both_filters_compose(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Save",
                    enclosing_method="Run",
                    enclosing_class="WriteBack")
        # Only WriteBack.Run -- 2 calls.
        assert len(r) == 2, r
        for _, t in r:
            assert "[in WriteBack.Run]" in t, t

    def test_no_match_when_method_name_wrong(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Save", enclosing_method="NoSuchMethod")
        assert r == [], r


class TestEnclosingFilterOnAccessesOn(unittest.TestCase):
    def test_filter_applies_to_accesses_on(self):
        # ``q_accesses_on`` dedupes by source row, so each access must be on
        # its own line for the test to count distinct hits.
        src = """class C {
            void A(Repo r) {
                r.Save();
                r.Touch();
            }
            void B(Repo r) {
                r.Save();
            }
        }"""
        b, tree, lines = _parse(src)
        all_r = q_accesses_on(b, tree, lines, "Repo")
        assert len(all_r) == 3, all_r
        a_only = q_accesses_on(b, tree, lines, "Repo", enclosing_method="A")
        assert len(a_only) == 2, a_only


class TestEnclosingFilterOnCasts(unittest.TestCase):
    def test_filter_applies_to_casts(self):
        src = """class C {
            void A() { int x = (int)42L; }
            void B() { int y = (int)99L; }
        }"""
        b, tree, lines = _parse(src)
        all_c = q_casts(b, tree, lines, "int")
        assert len(all_c) == 2, all_c
        a_only = q_casts(b, tree, lines, "int", enclosing_method="A")
        assert len(a_only) == 1, a_only


class TestEnclosingFilterOnAccessesOf(unittest.TestCase):
    def test_filter_applies_to_accesses_of(self):
        # Same one-access-per-line constraint as accesses_on.
        src = """class C {
            void A(Foo f) {
                var v = f.Value;
            }
            void B(Foo f) {
                var v = f.Value;
                var w = f.Value;
            }
        }"""
        b, tree, lines = _parse(src)
        all_r = q_accesses_of(b, tree, lines, "Value")
        assert len(all_r) == 3, all_r
        b_only = q_accesses_of(b, tree, lines, "Value", enclosing_method="B")
        assert len(b_only) == 2, b_only


class TestEnclosingFilterOnAllRefs(unittest.TestCase):
    def test_filter_applies_to_all_refs(self):
        src = """class C {
            void A() { var foo = 1; foo = 2; }
            void B() { var foo = 3; }
        }"""
        b, tree, lines = _parse(src)
        all_r = q_all_refs(b, tree, lines, "foo")
        # 3 unique row hits: line 2 (decl+assign on different rows), line 2 assign? Actually:
        #   Line 2: ``var foo = 1; foo = 2;`` -- single row, so 1 hit
        #   Line 3: ``var foo = 3;`` -- 1 hit
        # all_refs dedupes by row, so 2 hits total.
        assert len(all_r) == 2, all_r
        a_only = q_all_refs(b, tree, lines, "foo", enclosing_method="A")
        assert len(a_only) == 1, a_only


class TestEnclosingFilterOnUsesLocals(unittest.TestCase):
    def test_filter_applies_to_uses_locals(self):
        src = """class C {
            void A() { Repo r = null; r.Save(); }
            void B() { Repo r = null; r.Save(); }
        }"""
        b, tree, lines = _parse(src)
        all_l = q_uses(b, tree, lines, "Repo", uses_kind="locals")
        assert len(all_l) == 2, all_l
        a_only = q_uses(b, tree, lines, "Repo", uses_kind="locals",
                         enclosing_method="A")
        assert len(a_only) == 1, a_only


if __name__ == "__main__":
    unittest.main()
