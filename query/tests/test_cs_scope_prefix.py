"""
Tests for the enclosing-scope prefix on pattern-mode AST hits.

Each result emitted by ``calls`` / ``uses`` / ``accesses_of`` /
``accesses_on`` / ``casts`` is prepended with ``[in TypeName.MemberName] ``
(or ``[in TypeName] `` at type-level, or nothing at namespace level) so
agents can tell which class/method a hit lives in without a follow-up
``at LINE:COL`` query.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import (
    q_calls, q_accesses_of, q_accesses_on, q_casts, q_uses,
    _scope_prefix, _enclosing_member_name, _find_all,
)

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _parse(src: str):
    b = src.encode()
    tree = _PARSER.parse(b)
    return b, tree, src.splitlines()


_SRC = """\
namespace Acme {
    public class Widget {
        private int _count;
        public void DoWork() {
            Logger.Info("hi");
            _count = (int)42L;
        }
        public int Value { get { return _count; } }
    }
    public class Caller {
        public void Run(Widget w) {
            w.DoWork();
            var v = w.Value;
        }
    }
}
"""


class TestScopePrefixHelpers(unittest.TestCase):
    """Unit tests on the helper that builds the prefix."""

    def test_namespace_level_has_no_prefix(self):
        # No class around the node -> empty string.
        b, tree, _ = _parse("namespace N { }")
        root = tree.root_node
        assert _scope_prefix(root, b) == ""

    def test_inside_method_yields_class_dot_member(self):
        b, tree, _ = _parse(_SRC)
        # Find the invocation_expression for ``Logger.Info(...)`` -- it
        # lives inside ``Widget.DoWork``.
        calls = _find_all(tree.root_node, lambda n: n.type == "invocation_expression")
        assert calls, "expected at least one invocation in fixture"
        prefix = _scope_prefix(calls[0], b)
        assert prefix == "[in Widget.DoWork] ", prefix

    def test_field_declaration_name_resolves_via_declarator(self):
        # field_declaration has no direct ``name`` field; the enclosing-
        # member helper should still recover the variable name.
        b, tree, _ = _parse("class C { private int Foo = 0; }")
        fields = _find_all(tree.root_node, lambda n: n.type == "field_declaration")
        # Pick any descendant of the field to walk up from.
        descendant = next((c for c in fields[0].children if c.is_named), fields[0])
        assert _enclosing_member_name(descendant, b) == "Foo"


class TestQCallsScopePrefix(unittest.TestCase):
    def test_call_inside_method_carries_scope(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "Info")
        assert len(r) == 1, r
        _, text = r[0]
        assert text.startswith("[in Widget.DoWork] "), text

    def test_call_in_different_method_carries_its_own_scope(self):
        b, tree, lines = _parse(_SRC)
        r = q_calls(b, tree, lines, "DoWork")
        # Two hits: declaration matches no, the call site ``w.DoWork()`` is
        # inside Caller.Run.
        assert any("[in Caller.Run] " in t for _, t in r), r


class TestQAccessesOnScopePrefix(unittest.TestCase):
    def test_member_access_via_typed_local_includes_scope(self):
        b, tree, lines = _parse(_SRC)
        r = q_accesses_on(b, tree, lines, "Widget")
        # ``w.DoWork()`` and ``w.Value`` both inside Caller.Run.
        assert r, r
        for _, txt in r:
            assert txt.startswith("[in Caller.Run] "), txt


class TestQAccessesOfScopePrefix(unittest.TestCase):
    def test_property_read_inside_method_carries_scope(self):
        b, tree, lines = _parse(_SRC)
        r = q_accesses_of(b, tree, lines, "Value")
        # ``w.Value`` access happens inside Caller.Run.
        assert r, r
        assert any("[in Caller.Run] " in t for _, t in r), r


class TestQCastsScopePrefix(unittest.TestCase):
    def test_cast_inside_method_carries_scope(self):
        b, tree, lines = _parse(_SRC)
        # ``(int)42L`` lives inside Widget.DoWork.
        r = q_casts(b, tree, lines, "int")
        assert r, r
        _, txt = r[0]
        assert txt.startswith("[in Widget.DoWork] "), txt


class TestQUsesLocalsScopePrefix(unittest.TestCase):
    def test_typed_local_inside_method_carries_scope(self):
        # ``Widget w`` would normally be a parameter (handled separately by
        # the param uses_kind); use an explicit local for clarity.
        src = """class C {
            void M() {
                Widget local = null;
                local.DoWork();
            }
        }"""
        b, tree, lines = _parse(src)
        r = q_uses(b, tree, lines, "Widget", uses_kind="locals")
        assert r, r
        _, text = r[0]
        assert text.startswith("[in C.M] "), text


if __name__ == "__main__":
    unittest.main()
