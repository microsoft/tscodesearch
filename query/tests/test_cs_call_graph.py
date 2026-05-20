"""
Tests for ``caller_of`` / ``callee_of`` -- the call-graph neighbour modes.

``caller_of METHOD`` groups every call site of METHOD by the enclosing
method, returning one row per (TypeName.MemberName) caller with a count
of how many call sites that caller contains.

``callee_of METHOD`` walks the body of the method named METHOD and
returns one row per distinct callee with a count of invocations. Object-
creation expressions (``new T(...)``) are reported as callees of T.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import q_caller_of, q_callee_of

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _parse(src: str):
    b = src.encode()
    tree = _PARSER.parse(b)
    return b, tree, src.splitlines()


# ---------------------------------------------------------------------------
# caller_of
# ---------------------------------------------------------------------------


class TestCallerOf(unittest.TestCase):
    def test_groups_multiple_call_sites_into_one_caller(self):
        src = """class C {
            void A() {
                Foo();
                Foo();
                Foo();
            }
        }"""
        b, tree, lines = _parse(src)
        r = q_caller_of(b, tree, lines, "Foo")
        assert len(r) == 1, r
        _, text = r[0]
        assert text.startswith("[in C.A]")
        assert "3 call sites" in text

    def test_separate_callers_emit_separate_rows(self):
        src = """class C {
            void A() { Foo(); }
            void B() { Foo(); Foo(); }
        }"""
        b, tree, lines = _parse(src)
        r = q_caller_of(b, tree, lines, "Foo")
        assert len(r) == 2, r
        texts = [t for _, t in r]
        assert any("[in C.A]" in t and "1 call site)" in t for t in texts), texts
        assert any("[in C.B]" in t and "2 call sites)" in t for t in texts), texts

    def test_empty_when_no_calls(self):
        src = "class C { void A() {} }"
        b, tree, lines = _parse(src)
        assert q_caller_of(b, tree, lines, "NoSuchMethod") == []

    def test_results_sorted_by_line(self):
        src = """class C {
            void Z() { Foo(); }
            void A() { Foo(); }
        }"""
        b, tree, lines = _parse(src)
        r = q_caller_of(b, tree, lines, "Foo")
        lns = [ln for ln, _ in r]
        assert lns == sorted(lns), lns


# ---------------------------------------------------------------------------
# callee_of
# ---------------------------------------------------------------------------


class TestCalleeOf(unittest.TestCase):
    def test_groups_distinct_callees_with_counts(self):
        src = """class C {
            void Driver() {
                Repo.Save();
                Repo.Save();
                Logger.Info("hi");
            }
        }"""
        b, tree, lines = _parse(src)
        r = q_callee_of(b, tree, lines, "Driver")
        # Two distinct callees inside Driver: Save (2x), Info (1x).
        texts = [t for _, t in r]
        assert any("Save" in t and "2 invocations" in t for t in texts), texts
        assert any("Info" in t and "1 invocation)" in t for t in texts), texts

    def test_constructor_calls_reported_as_ctor(self):
        src = """class C {
            void Make() {
                var w = new Widget();
                var g = new Gadget();
                var w2 = new Widget();
            }
        }"""
        b, tree, lines = _parse(src)
        r = q_callee_of(b, tree, lines, "Make")
        texts = [t for _, t in r]
        assert any("Widget" in t and "2 invocations, ctor" in t for t in texts), texts
        assert any("Gadget" in t and "1 invocation, ctor" in t for t in texts), texts

    def test_only_searches_the_named_method_body(self):
        src = """class C {
            void Target() { Inside(); }
            void Other()  { NotInTarget(); }
        }"""
        b, tree, lines = _parse(src)
        r = q_callee_of(b, tree, lines, "Target")
        texts = " ".join(t for _, t in r)
        assert "Inside" in texts
        assert "NotInTarget" not in texts

    def test_empty_when_method_not_found(self):
        src = "class C { void M() { Foo(); } }"
        b, tree, lines = _parse(src)
        assert q_callee_of(b, tree, lines, "DoesNotExist") == []

    def test_handles_conditional_access_call(self):
        # ``obj?.Method(...)`` should still be picked up as a callee.
        src = """class C {
            void Run(Repo r) {
                r?.Save();
            }
        }"""
        b, tree, lines = _parse(src)
        r = q_callee_of(b, tree, lines, "Run")
        texts = " ".join(t for _, t in r)
        assert "Save" in texts, texts


if __name__ == "__main__":
    unittest.main()
