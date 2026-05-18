"""
Tests for the C# ``at`` (position lookup) and ``body`` (member source) modes.

``at LINE:COL`` finds the deepest AST node at a position and reports the
chain of enclosing named declarations -- the agent uses this to resolve
stack traces, test failures, and review comments that point at a file:line.

``body NAME`` returns the full source of every member declaration named
NAME -- sugar for ``declarations`` with ``include_body=True``.
"""
from __future__ import annotations

import unittest

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from query.cs import q_at, q_body

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


_SRC = """\
namespace Acme.Billing {

    public class Widget {
        private string _name;

        public void SaveChanges() {
            var store = new Repository();
            store.Persist(_name);
        }

        public string Name { get; set; }
    }

    public class Caller {
        public void Run(Widget w) {
            w.SaveChanges();
        }
    }
}
"""


def _parse():
    b = _SRC.encode()
    tree = _PARSER.parse(b)
    return b, tree, _SRC.splitlines()


# -- q_at ----------------------------------------------------------------------

class TestQAt(unittest.TestCase):
    """``at`` returns the deepest node at a position plus its enclosing scopes."""

    def setUp(self):
        self.fx = _parse()

    def test_at_method_name_resolves_to_method_scope(self):
        # Line 6 is `public void SaveChanges() {` -- point at the `S` of SaveChanges.
        results = q_at(*self.fx, "6:21")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("identifier", text)
        self.assertIn("SaveChanges", text)
        # Scope chain should include the method and the containing class.
        self.assertIn("[method]", text)
        self.assertIn("[class] Widget", text)
        self.assertIn("[namespace] Acme.Billing", text)

    def test_at_inside_method_body_reports_method_scope(self):
        # Line 8: `store.Persist(_name);` -- point at `Persist`.
        results = q_at(*self.fx, "8:19")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("Persist", text)
        # Innermost named scope is the method, then the class.
        method_idx = text.find("[method]")
        class_idx  = text.find("[class]")
        self.assertGreater(method_idx, -1, f"missing method scope: {text}")
        self.assertGreater(class_idx, -1, f"missing class scope: {text}")
        self.assertLess(method_idx, class_idx,
                        "method scope should come before class scope (innermost-first)")

    def test_at_property_resolves_to_property_scope(self):
        # Line 11: `public string Name { get; set; }` -- point at `Name`.
        # ``_node_kind`` produces ``[property]`` for property_declaration nodes
        # (the long form); ``q_methods`` uses ``[prop]`` as a shorthand.
        results = q_at(*self.fx, "11:23")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("Name", text)
        self.assertIn("[property]", text)

    def test_at_class_keyword_resolves_to_class_scope(self):
        # Line 3: `public class Widget {` -- point inside the class name.
        results = q_at(*self.fx, "3:18")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("Widget", text)
        # Class scope is the innermost named scope at that point.
        self.assertIn("[class] Widget", text)

    def test_at_returns_empty_for_invalid_position(self):
        # 999 is past EOF; q_at should gracefully return no match.
        results = q_at(*self.fx, "999:1")
        self.assertEqual(results, [])

    def test_at_returns_empty_for_malformed_position(self):
        # Garbage input shouldn't crash.
        self.assertEqual(q_at(*self.fx, "notaposition"), [])
        self.assertEqual(q_at(*self.fx, ""), [])

    def test_at_namespace_outside_any_class(self):
        # Line 1: `namespace Acme.Billing {` -- point at the namespace name.
        results = q_at(*self.fx, "1:13")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("[namespace] Acme.Billing", text)
        # No class/method scope at this position.
        self.assertNotIn("[class]", text)
        self.assertNotIn("[method]", text)


# -- q_at: field/event scope-chain regression ----------------------------------


_FIELDS_SRC = """\
namespace Acme {
    public class Container {
        private int _count = 0;
        private static readonly System.Guid SettingsKS = System.Guid.NewGuid();
        private string _first, _second;
        public event System.Action OnChange;
    }
}
"""


class TestQAtInsideFieldDeclarations(unittest.TestCase):
    """Regression: ``field_declaration`` and ``event_field_declaration``
    don't expose a direct ``name`` field -- the name lives inside a nested
    ``variable_declarator``. q_at must still report the field in the
    enclosing-scope chain rather than silently skipping it."""

    def setUp(self):
        b = _FIELDS_SRC.encode()
        tree = _PARSER.parse(b)
        self.fx = (b, tree, _FIELDS_SRC.splitlines())

    def test_at_inside_simple_field(self):
        # Line 3: `private int _count = 0;` -- cursor on `_count`.
        text = q_at(*self.fx, "3:24")[0][-1]
        self.assertIn("[field] _count", text)
        self.assertIn("[class] Container", text)

    def test_at_on_field_modifier_keyword(self):
        # Cursor on the `readonly` keyword still resolves to the field's
        # scope -- fall-back picks the first declarator's name.
        text = q_at(*self.fx, "4:24")[0][-1]
        self.assertIn("[field] SettingsKS", text)

    def test_at_inside_multi_declarator_field_picks_correct_one(self):
        # `private string _first, _second;` -- point at `_second`.
        text = q_at(*self.fx, "5:32")[0][-1]
        self.assertIn("[field] _second", text)
        # And pointing at `_first` picks _first.
        text = q_at(*self.fx, "5:24")[0][-1]
        self.assertIn("[field] _first", text)

    def test_at_inside_event_field(self):
        # `public event System.Action OnChange;` -- cursor on `OnChange`.
        text = q_at(*self.fx, "6:36")[0][-1]
        self.assertIn("[event field] OnChange", text)


# -- q_body --------------------------------------------------------------------

class TestQBody(unittest.TestCase):
    """``body NAME`` returns the full source of every declaration named NAME."""

    def setUp(self):
        self.fx = _parse()

    def test_body_returns_full_method_source(self):
        results = q_body(*self.fx, "SaveChanges")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        # Body content (the actual method statements) must be in the response.
        self.assertIn("var store = new Repository()", text)
        self.assertIn("store.Persist(_name)", text)

    def test_body_returns_class_source_for_a_type_name(self):
        results = q_body(*self.fx, "Widget")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        # The whole class block should be there -- header + every member.
        self.assertIn("public class Widget", text)
        self.assertIn("SaveChanges", text)
        self.assertIn("Name { get; set; }", text)

    def test_body_returns_empty_for_unknown_name(self):
        self.assertEqual(q_body(*self.fx, "NoSuchMember"), [])

    def test_body_with_symbol_kind_restricts_results(self):
        # Restricting to the `method` kind returns only the SaveChanges
        # method, not the class named SaveChanges (none here, but the
        # filter must still narrow correctly).
        results = q_body(*self.fx, "SaveChanges", symbol_kind="method")
        self.assertEqual(len(results), 1)
        text = results[0][-1]
        self.assertIn("[method]", text)


if __name__ == "__main__":
    unittest.main()
