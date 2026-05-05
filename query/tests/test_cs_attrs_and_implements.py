"""
Tests for q_attrs and q_implements using sample/root1/Processors.cs.

Replicates behavior observed in Round 3 of guided testing:

  attrs
    → arguments were always dropped: child_by_field_name("arguments") returns
      None — the tree-sitter C# grammar attaches the argument list as a named
      child with type "attribute_argument_list", not via a field name.
      Fixed by scanning named_children for "attribute_argument_list".

  implements
    → generic base types displayed without type parameters:
      _base_type_names returns only the bare identifier from generic_name
      nodes (e.g. "IProcessor" not "IProcessor<string>") because that is
      what Typesense needs for indexing.  The display in q_implements and
      _q_base_uses used those stripped names verbatim.
      Fixed by reading the raw base_list node text for display while keeping
      _base_type_names unchanged for matching and indexing.

Run (no Typesense needed):
    pytest query/tests/test_cs_attrs_and_implements.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import q_attrs, q_implements, q_uses

_PROC = os.path.join(SAMPLE_ROOT1, "Processors.cs")

with open(_PROC, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)


def _texts(results):
    return " ".join(t for _, t in results)

def _lines(results):
    return {ln for ln, _ in results}


# ===========================================================================
# q_attrs — argument capture (was broken)
# ===========================================================================

class TestAttrs(unittest.TestCase):
    """
    q_attrs used child_by_field_name("arguments") to get the attribute
    argument list.  The tree-sitter C# grammar gives the argument list node
    type "attribute_argument_list" but attaches it as an unnamed child (no
    field name), so the lookup always returned None and arguments were lost.
    """

    def _attrs(self, name=None):
        return q_attrs(*_PARSED, attr_name=name)

    def test_obsolete_argument_captured(self):
        """[Obsolete("Use EnhancedProcessor instead")] — argument must appear."""
        r = self._attrs("Obsolete")
        assert r, "Obsolete attribute must be found"
        texts = _texts(r)
        assert "Use EnhancedProcessor instead" in texts, \
            f"Obsolete argument text missing: {r}"

    def test_obsolete_argument_has_parens(self):
        r = self._attrs("Obsolete")
        texts = _texts(r)
        assert "(" in texts and ")" in texts, \
            f"Argument parentheses missing: {r}"

    def test_no_argument_attribute_unaffected(self):
        """[Serializable] has no arguments — must still be found, no crash."""
        r = self._attrs("Serializable")
        assert len(r) == 2, f"Expected 2 [Serializable] hits, got {r}"
        for _, t in r:
            assert t == "[Serializable]", \
                f"No-arg attribute must format as '[Name]', got: {t!r}"

    def test_listing_all_attrs_includes_both(self):
        """Without a filter, all attributes in the file are returned."""
        r = self._attrs()
        names = {t.split("]")[0].lstrip("[") for _, t in r}
        assert "Serializable" in names
        assert "Obsolete"     in names

    def test_nonexistent_attr_returns_empty(self):
        assert self._attrs("NoSuchAttr") == []

    def test_attribute_suffix_stripped(self):
        """[SerializableAttribute] and [Serializable] are the same attribute."""
        src = """\
namespace Synth {
    [SerializableAttribute]
    public class C { }
}
"""
        from tests.base import _parse as p
        r = q_attrs(*p(src), attr_name="Serializable")
        assert r, "SerializableAttribute must match 'Serializable' search"


# ===========================================================================
# q_implements — generic base type display (was degraded)
# ===========================================================================

class TestImplementsDisplay(unittest.TestCase):
    """
    q_implements used _base_type_names() for the display string.
    _base_type_names() strips generic type parameters (returning "IProcessor"
    instead of "IProcessor<string>") because that is what the Typesense
    index needs.  The result text therefore omitted type parameters, which
    was misleading when multiple implementations used different type args.
    Fixed by reading the raw base_list node text for display.
    """

    def _impl(self, name):
        return q_implements(*_PARSED, type_name=name)

    def test_generic_type_param_shown_in_result(self):
        """TextProcessor : BaseProcessor<string>, IProcessor<string> — '<string>' must appear."""
        r = self._impl("IProcessor")
        texts = _texts(r)
        assert "<string>" in texts, \
            f"Generic type parameter '<string>' missing from display: {r}"

    def test_base_processor_shows_type_param(self):
        """BaseProcessor<T> : IProcessor<T> — type param T must appear."""
        r = self._impl("IProcessor")
        base_hits = [(ln, t) for ln, t in r if "BaseProcessor" in t]
        assert base_hits, "BaseProcessor must appear in IProcessor results"
        _, text = base_hits[0]
        assert "<T>" in text, f"'<T>' missing from BaseProcessor display: {text!r}"

    def test_matching_still_works_with_bare_name(self):
        """Searching for 'IProcessor' (no type args) must still find both implementors."""
        r = self._impl("IProcessor")
        assert len(r) == 2, f"Expected 2 IProcessor implementors, got {r}"
        texts = _texts(r)
        assert "BaseProcessor" in texts
        assert "TextProcessor" in texts

    def test_nongeneric_base_unaffected(self):
        """Non-generic base classes must still display correctly."""
        src = """\
namespace Synth {
    public class Base { }
    public class Child : Base { }
}
"""
        from tests.base import _parse as p
        r = q_implements(*p(src), type_name="Base")
        assert r, "Child : Base must be found"
        _, text = r[0]
        assert "Base" in text
        assert "<" not in text, f"Non-generic base must not show angle brackets: {text!r}"

    def test_uses_kind_base_also_shows_generic_params(self):
        """uses_kind=base uses the same _q_base_uses function — must also show generics."""
        r = q_uses(*_PARSED, type_name="IProcessor", uses_kind="base")
        assert r, "uses_kind=base must find IProcessor implementors"
        texts = _texts(r)
        assert "<" in texts, \
            f"Generic type params must appear in uses_kind=base display: {r}"


if __name__ == "__main__":
    unittest.main()
