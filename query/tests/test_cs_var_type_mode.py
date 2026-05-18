"""
Tests for the ``var_type NAME`` query mode (C#).

Exercises the user-facing wrapper around ``_VarTypeMap.resolve_at``: for
each occurrence of NAME in a file, report the resolved type at that scope
or a sentinel when the resolver can't pin it down.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import q_var_type

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _run(src: str, name: str):
    b = src.encode()
    tree = _PARSER.parse(b)
    return q_var_type(b, tree, src.splitlines(), name)


def _text_of(results):
    return [t for _, t in results]


class TestQVarTypeResolves(unittest.TestCase):
    """Distinct scopes should report distinct types for the same name."""

    def test_parameter_typed(self):
        src = "class C { void M(Repo r) { r.Save(); } }"
        out = _run(src, "r")
        # Both occurrences (parameter list + body) resolve to Repo.
        assert any("r : Repo" in t for t in _text_of(out)), out
        assert all(": (unresolved)" not in t and ": (conflicting)" not in t
                   for t in _text_of(out)), out

    def test_typed_local_in_block(self):
        src = "class C { void M() { Customer c = null; c.Touch(); } }"
        out = _run(src, "c")
        assert any("c : Customer" in t for t in _text_of(out)), out

    def test_different_methods_get_different_types(self):
        # Method-scope isolation: same name, different types, no conflict.
        src = """class C {
            void A() { Repo r = null; r.Save(); }
            void B() { Customer r = null; r.Touch(); }
        }"""
        out = _run(src, "r")
        types_per_line = {ln: txt for ln, txt in out}
        # Line 2 inside A — Repo.
        assert "Repo" in types_per_line[2], types_per_line
        # Line 3 inside B — Customer.
        assert "Customer" in types_per_line[3], types_per_line


class TestQVarTypeUnresolved(unittest.TestCase):
    """Unknown names report ``(unresolved)``; ambiguous-in-scope reports
    ``(conflicting)`` so an agent can tell the two situations apart."""

    def test_never_declared(self):
        src = "class C { void M() { unknown.Method(); } }"
        out = _run(src, "unknown")
        assert any(": (unresolved)" in t for t in _text_of(out)), out

    def test_conflicting_redeclaration_in_same_block(self):
        src = """class C {
            void M() {
                Repo x = null;
                x.Save();
                Customer x = null;
                x.Touch();
            }
        }"""
        out = _run(src, "x")
        # Every emitted entry for x in this single (conflicted) scope is
        # labelled "(conflicting)" — we never invent a winning type.
        assert all(": (conflicting)" in t for t in _text_of(out)), out


class TestQVarTypeInferenceHeuristics(unittest.TestCase):
    """The var-type map's inference heuristics should propagate to
    ``var_type`` — e.g. ``var x = new Foo()`` reports x as Foo."""

    def test_var_from_new(self):
        src = "class C { void M() { var w = new Widget(); w.Render(); } }"
        out = _run(src, "w")
        assert any("w : Widget" in t for t in _text_of(out)), out

    def test_var_from_generic_method(self):
        src = "class C { void M(IContainer c) { var s = c.Resolve<IService>(); s.Run(); } }"
        out = _run(src, "s")
        assert any("s : IService" in t for t in _text_of(out)), out


class TestQVarTypeSkipsLiterals(unittest.TestCase):
    """Identifiers inside strings/comments are not matched (mirrors
    ``all_refs``)."""

    def test_string_mention_excluded(self):
        src = '''class C { void M() {
            Repo r = null;
            string s = "use r for storage";
            r.Save();
        } }'''
        out = _run(src, "r")
        # Only the real declaration + use line — not the inside of the string.
        rows = [ln for ln, _ in out]
        assert 2 in rows and 4 in rows, out
        assert 3 not in rows, out


if __name__ == "__main__":
    unittest.main()
