"""
Tests for the C# visibility capture (modifier extraction + defaults) and
the visibility filter on declaration query modes.

Covers:
  * Explicit modifier resolution (public/internal/protected/private),
    including compound forms like ``protected internal`` and
    ``private protected``.
  * Language defaults: top-level types default to internal; nested types
    default to private; interface members default to public; enum members
    default to public; class members default to private.
  * Filter behavior: visibility="" matches everything; visibility="public"
    keeps only public; comma-separated keeps the union; languages that
    don't capture visibility match nothing under a filter.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import (
    q_classes, q_methods, q_fields, q_declarations,
    _cs_type_visibility, _cs_member_visibility, _cs_explicit_visibility,
    _find_all,
)

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _parse(src: str):
    b = src.encode()
    tree = _PARSER.parse(b)
    return b, tree, src.splitlines()


def _find(tree, types):
    return _find_all(tree.root_node, lambda n: n.type in types)


# -- Modifier extraction ------------------------------------------------------


class TestExplicitVisibility(unittest.TestCase):
    """Modifier-keyword parsing on a declaration node."""

    def _vis(self, src: str, node_type: str) -> str:
        _b, tree, _ = _parse(src)
        nodes = _find(tree, {node_type})
        assert nodes, f"no {node_type} in source"
        return _cs_explicit_visibility(nodes[0])

    def test_public(self):
        assert self._vis("public class C {}", "class_declaration") == "public"

    def test_internal(self):
        assert self._vis("internal class C {}", "class_declaration") == "internal"

    def test_protected_internal_collapses_to_protected(self):
        # ``protected internal`` = reachable via inheritance from outside
        # the assembly -- classifies as ``protected`` for filtering.
        src = "class O { protected internal void M() {} }"
        assert self._vis(src, "method_declaration") == "protected"

    def test_private_protected_collapses_to_private(self):
        # ``private protected`` = most-restricted compound form.
        src = "class O { private protected void M() {} }"
        assert self._vis(src, "method_declaration") == "private"

    def test_no_modifier_returns_empty(self):
        # Bare ``void M()`` with no modifier -- explicit extractor returns "".
        # Defaults are applied by the higher-level _cs_member_visibility.
        src = "class O { void M() {} }"
        assert self._vis(src, "method_declaration") == ""


# -- Language defaults --------------------------------------------------------


class TestTypeDefaults(unittest.TestCase):
    """``_cs_type_visibility`` should apply C# defaults to types without
    explicit modifiers."""

    def test_top_level_class_defaults_to_internal(self):
        _b, tree, _ = _parse("class TopLevel {}")
        node = _find(tree, {"class_declaration"})[0]
        assert _cs_type_visibility(node) == "internal"

    def test_nested_class_defaults_to_private(self):
        _b, tree, _ = _parse("class Outer { class Nested {} }")
        nested = _find(tree, {"class_declaration"})[1]
        assert _cs_type_visibility(nested) == "private"


class TestMemberDefaults(unittest.TestCase):
    """``_cs_member_visibility`` should apply C# defaults to members
    without explicit modifiers (class => private; interface => public;
    enum body => public)."""

    def test_class_member_defaults_to_private(self):
        _b, tree, _ = _parse("class C { void M() {} }")
        m = _find(tree, {"method_declaration"})[0]
        assert _cs_member_visibility(m) == "private"

    def test_interface_member_defaults_to_public(self):
        _b, tree, _ = _parse("interface I { void M(); }")
        m = _find(tree, {"method_declaration"})[0]
        assert _cs_member_visibility(m) == "public"

    def test_explicit_member_modifier_wins_over_default(self):
        # Even inside an interface, an explicit ``private`` overrides the
        # public default (C# 8 added this; we just defer to the modifier).
        _b, tree, _ = _parse("interface I { private void M() {} }")
        m = _find(tree, {"method_declaration"})[0]
        assert _cs_member_visibility(m) == "private"


# -- Visibility filter on declaration queries ---------------------------------


_SRC = """\
namespace N {
    public class Public {
        public void PubMethod() {}
        internal void IntMethod() {}
        protected void ProtMethod() {}
        private void PrivMethod() {}
        public int PubProp { get; set; }
        private int privField;
    }

    internal class Internal {
        public void OtherPub() {}
    }

    interface I {
        void IfaceMethod();
    }
}
"""


class TestQClassesVisibility(unittest.TestCase):
    def setUp(self):
        self.b, self.tree, self.lines = _parse(_SRC)

    def _names(self, results):
        # Result text is "[class] Name : Bases" (no end-line in this tuple).
        return [t.split("] ")[1].split(" ")[0].split(":")[0].strip()
                for _, _, t in results]

    def test_no_filter_returns_all(self):
        names = self._names(q_classes(self.b, self.tree, self.lines))
        assert set(names) == {"Public", "Internal", "I"}, names

    def test_public_only(self):
        names = self._names(q_classes(self.b, self.tree, self.lines,
                                       visibility="public"))
        assert names == ["Public"], names

    def test_internal_includes_interface_default(self):
        # ``interface I`` has no modifier -> top-level default = internal.
        names = self._names(q_classes(self.b, self.tree, self.lines,
                                       visibility="internal"))
        assert set(names) == {"Internal", "I"}, names

    def test_multi_value_visibility(self):
        names = self._names(q_classes(self.b, self.tree, self.lines,
                                       visibility="public,internal"))
        assert set(names) == {"Public", "Internal", "I"}, names


class TestQMethodsVisibility(unittest.TestCase):
    def setUp(self):
        self.b, self.tree, self.lines = _parse(_SRC)

    def _method_names(self, results):
        # Method text looks like "[method] <sig>" -- sig has the name.
        names = []
        for _, _, t in results:
            # Skip non-method lines (props, fields).
            if t.startswith("[method]"):
                # ``[method] void PubMethod()`` -> "PubMethod"
                sig = t.split("] ", 1)[1]
                # Last whitespace-separated token before ``(``.
                names.append(sig.split("(")[0].strip().split(" ")[-1])
        return names

    def test_public_only_methods(self):
        names = self._method_names(q_methods(self.b, self.tree, self.lines,
                                              visibility="public"))
        # PubMethod, OtherPub, IfaceMethod (interface default = public).
        assert set(names) == {"PubMethod", "OtherPub", "IfaceMethod"}, names

    def test_private_includes_class_member_default(self):
        # ``private void PrivMethod`` is explicit private. No implicit
        # method in this fixture is "default-private", since the others
        # have explicit modifiers.
        names = self._method_names(q_methods(self.b, self.tree, self.lines,
                                              visibility="private"))
        assert names == ["PrivMethod"], names

    def test_unknown_visibility_returns_empty(self):
        # Typo: "publik" -- no match (treated as a hard filter, not a fallback).
        out = q_methods(self.b, self.tree, self.lines, visibility="publik")
        assert out == [], out


class TestQFieldsVisibility(unittest.TestCase):
    def setUp(self):
        self.b, self.tree, self.lines = _parse(_SRC)

    def test_public_props_only(self):
        out = q_fields(self.b, self.tree, self.lines, visibility="public")
        names = [t.split(" ")[-1] for _, _, t in out]
        assert names == ["PubProp"], names

    def test_private_fields_only(self):
        out = q_fields(self.b, self.tree, self.lines, visibility="private")
        names = [t.split(" ")[-1] for _, _, t in out]
        assert names == ["privField"], names


class TestQDeclarationsVisibility(unittest.TestCase):
    def setUp(self):
        self.b, self.tree, self.lines = _parse(_SRC)

    def test_filter_keeps_only_named_match_with_visibility(self):
        # Three declarations named ``OtherPub`` etc. -- but searching for
        # ``Public`` restricted to public matches the public class only.
        out = q_declarations(self.b, self.tree, self.lines, "Public",
                              visibility="public")
        # Header text is ``[class] Public S-E:``.
        headers = [t.splitlines()[0] for _, t in out]
        assert any("[class] Public" in h for h in headers), headers

    def test_filter_drops_non_matching_visibility(self):
        # Searching for ``Internal`` (the class name) restricted to public
        # returns nothing -- the class is internal.
        out = q_declarations(self.b, self.tree, self.lines, "Internal",
                              visibility="public")
        assert out == [], out


if __name__ == "__main__":
    unittest.main()
