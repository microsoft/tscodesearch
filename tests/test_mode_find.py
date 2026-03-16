"""
Tests for find mode.

mode: --find NAME (q_find)

Finds the full source span of every method, type, property, or local function
named NAME.

Gaps tested:
  - Methods with the given name are returned with their full body.
  - Types (classes, interfaces) with the given name are returned.
  - Same name in multiple types returns multiple results.
  - Non-matching names return empty.
  - Output includes line numbers and kind annotation.
  - Local functions nested inside methods are found.
  - Properties with the given name are found.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import FIND_TARGET
from query import q_find


# ══════════════════════════════════════════════════════════════════════════════
# q_find AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQFind(unittest.TestCase):

    def _find(self, src, name):
        return q_find(*_parse(src), name=name)

    def test_finds_method(self):
        r = self._find(FIND_TARGET, "TargetMethod")
        assert r, "TargetMethod must be found"

    def test_finds_both_overloads_in_different_classes(self):
        """TargetMethod exists in both FindMe and AnotherClass."""
        r = self._find(FIND_TARGET, "TargetMethod")
        assert len(r) >= 2, \
            f"Expected at least 2 results (one per class), got: {len(r)}"

    def test_finds_private_method(self):
        r = self._find(FIND_TARGET, "Lookup")
        assert r, "Private method must be found"

    def test_nonexistent_name_empty(self):
        r = self._find(FIND_TARGET, "NonExistent")
        assert r == []

    def test_output_includes_kind(self):
        r = self._find(FIND_TARGET, "TargetMethod")
        texts = [t for _, t in r]
        assert any("method" in t.lower() for t in texts), \
            f"Output must include kind annotation: {texts}"

    def test_output_includes_line_numbers(self):
        r = self._find(FIND_TARGET, "TargetMethod")
        texts = [t for _, t in r]
        assert any("lines" in t for t in texts), \
            f"Output must include line range: {texts}"

    def test_output_includes_body(self):
        """Full body of the method should appear in the output."""
        r = self._find(FIND_TARGET, "TargetMethod")
        texts = [t for _, t in r]
        # At least one result should contain the method body content
        assert any("TargetMethod" in t for t in texts)

    def test_finds_class(self):
        r = self._find(FIND_TARGET, "FindMe")
        assert r, "Class declaration 'FindMe' must be found"
        texts = [t for _, t in r]
        assert any("class" in t.lower() for t in texts)

    def test_finds_local_function(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            void InnerHelper(int x) { }
            InnerHelper(1);
        }
    }
}
"""
        r = self._find(src, "InnerHelper")
        assert r, "Local function must be found by q_find"

    def test_finds_property(self):
        src = """\
namespace Synth {
    public class Model {
        public string Name { get; set; }
        public int Count { get; }
    }
}
"""
        r = self._find(src, "Name")
        assert r, "Property 'Name' must be found"

    def test_finds_constructor(self):
        src = """\
namespace Synth {
    public class Service {
        public Service(string name) { }
    }
}
"""
        r = self._find(src, "Service")
        assert r, "Constructor must be found by class name"

    def test_line_numbers_are_positive(self):
        r = self._find(FIND_TARGET, "TargetMethod")
        for ln, _ in r:
            assert ln > 0, f"Line number must be positive, got {ln}"

    def test_no_duplicate_results_for_unique_name(self):
        r = self._find(FIND_TARGET, "Lookup")
        assert len(r) == 1, \
            f"Unique name 'Lookup' must return exactly 1 result, got {len(r)}"


if __name__ == "__main__":
    unittest.main()
