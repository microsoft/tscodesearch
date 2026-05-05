"""
Unit tests for attr mode.

Typesense field: attr_names
search_code query_by: attr_names,filename
mode: --attrs [NAME] (q_attrs)

Gaps tested:
  - Only actual [Attribute] decorations populate the attr_names field.
  - 'Attribute' suffix is stripped ([SerializableAttribute] → 'Serializable').
  - Attribute names in comments and string literals are NOT indexed.
  - Attributes on methods/properties are also indexed (not just classes).
  - The 'attr_names' field must not bleed into type_refs or member_sigs.

Integration tests (require Typesense) are in tests/integration/test_mode_attr.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from tests.base import _parse
from tests.fixtures import (
    HAS_CACHEABLE_ATTR, HAS_OBSOLETE_NOT_CACHEABLE, NO_ATTRS,
)
from indexserver.indexer import extract_metadata, build_document
from query.cs import q_attrs


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — attributes field
# ══════════════════════════════════════════════════════════════════════════════

class TestAttributesField(unittest.TestCase):

    def test_attribute_indexed(self):
        meta = extract_metadata(HAS_CACHEABLE_ATTR.encode(), ".cs")
        assert "Cacheable" in meta["attr_names"], \
            f"attr_names: {meta['attr_names']}"

    def test_wrong_attribute_not_indexed(self):
        meta = extract_metadata(HAS_OBSOLETE_NOT_CACHEABLE.encode(), ".cs")
        assert "Cacheable" not in meta["attr_names"]

    def test_no_attributes_empty(self):
        meta = extract_metadata(NO_ATTRS.encode(), ".cs")
        assert meta["attr_names"] == []

    def test_suffix_stripped(self):
        src = """\
namespace Synth {
    [SerializableAttribute]
    public class Payload { }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "Serializable" in meta["attr_names"], \
            f"attr_names: {meta['attr_names']}"

    def test_attribute_in_string_not_indexed(self):
        src = """\
namespace Synth {
    public class Doc {
        public string Note = \"[Cacheable] attribute description\";
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "Cacheable" not in meta["attr_names"]

    def test_attribute_in_comment_not_indexed(self):
        src = """\
namespace Synth {
    // Use [Cacheable] for hot paths
    public class Worker {
        public void DoWork() { }
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "Cacheable" not in meta["attr_names"]

    def test_multiple_attributes_all_indexed(self):
        src = """\
namespace Synth {
    [Cacheable]
    [Serializable]
    [Obsolete]
    public class Multi { }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        for attr in ("Cacheable", "Serializable", "Obsolete"):
            assert attr in meta["attr_names"], f"'{attr}' missing: {meta['attr_names']}"

    def test_method_level_attribute(self):
        src = """\
namespace Synth {
    public class Controller {
        [TestMethod]
        public void MyTest() { }
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "TestMethod" in meta["attr_names"]

    def test_property_level_attribute(self):
        src = """\
namespace Synth {
    public class Model {
        [Required]
        public string Name { get; set; }
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "Required" in meta["attr_names"]

    def test_attribute_args_not_in_attr_names_list(self):
        """Attribute arguments (like ttl: 60) must not appear as attribute names."""
        meta = extract_metadata(HAS_CACHEABLE_ATTR.encode(), ".cs")
        assert "ttl" not in meta["attr_names"]
        assert "60"  not in meta["attr_names"]

    def test_attr_names_not_in_type_refs(self):
        """Attribute names must not contaminate type_refs."""
        meta = extract_metadata(HAS_CACHEABLE_ATTR.encode(), ".cs")
        assert "Cacheable" not in meta["type_refs"]

    def test_build_document_tokens_includes_attribute(self):
        """build_document tokens field has the raw source, so attr name is findable
        via text mode even if it's only in a comment."""
        src = """\
namespace Synth {
    // Use [Cacheable] for hot paths
    public class HotPath {
        public void Execute() { }
    }
}
"""
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False, mode="w") as f:
            f.write(src)
            tmp = f.name
        try:
            doc = build_document(tmp, "synth/HotPath.cs")
            assert "Cacheable" not in doc.get("attr_names", [])
            assert "Cacheable" in doc["tokens"]
        finally:
            os.unlink(tmp)


# ══════════════════════════════════════════════════════════════════════════════
# q_attrs AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQAttrs(unittest.TestCase):

    def _attrs(self, src, name=None):
        return q_attrs(*_parse(src), attr_name=name)

    def test_finds_attribute(self):
        r = self._attrs(HAS_CACHEABLE_ATTR, "Cacheable")
        assert r, "Expected [Cacheable] to be found"

    def test_no_filter_returns_all(self):
        src = """\
namespace Synth {
    [Cacheable]
    [Serializable]
    public class Multi { }
}
"""
        r = self._attrs(src)
        names = {t.split("]")[0].lstrip("[") for _, t in r}
        assert "Cacheable"    in names
        assert "Serializable" in names

    def test_filter_excludes_other_attrs(self):
        r = self._attrs(HAS_OBSOLETE_NOT_CACHEABLE, "Cacheable")
        assert r == []

    def test_no_attrs_returns_empty(self):
        r = self._attrs(NO_ATTRS, "Cacheable")
        assert r == []

    def test_suffix_stripped_in_filter(self):
        src = """\
namespace Synth {
    [SerializableAttribute]
    public class P { }
}
"""
        # Filter by short name
        r = self._attrs(src, "Serializable")
        assert r, "Filter by short name must match [SerializableAttribute]"

    def test_comment_not_returned(self):
        src = """\
namespace Synth {
    // [Cacheable]
    public class C { }
}
"""
        r = self._attrs(src, "Cacheable")
        assert r == []

    def test_method_attribute_found(self):
        src = """\
namespace Synth {
    public class T {
        [TestMethod]
        public void Run() { }
    }
}
"""
        r = self._attrs(src, "TestMethod")
        assert r, "Method-level attribute must be found"

    def test_output_includes_attribute_name(self):
        r = self._attrs(HAS_CACHEABLE_ATTR, "Cacheable")
        texts = [t for _, t in r]
        assert any("Cacheable" in t for t in texts)


if __name__ == "__main__":
    unittest.main()
