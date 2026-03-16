"""
Tests for attr mode.

Typesense field: attributes
search_code query_by: attributes,filename
mode: --attrs [NAME] (q_attrs)

Gaps tested:
  - Only actual [Attribute] decorations populate the attributes field.
  - 'Attribute' suffix is stripped ([SerializableAttribute] → 'Serializable').
  - Attribute names in comments and string literals are NOT indexed.
  - Attributes on methods/properties are also indexed (not just classes).
  - The 'attributes' field must not bleed into type_refs or method_sigs.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    HAS_CACHEABLE_ATTR, HAS_OBSOLETE_NOT_CACHEABLE, NO_ATTRS,
)
from tests.helpers import _server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_cs_metadata, build_document, run_index
from query import q_attrs


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — attributes field
# ══════════════════════════════════════════════════════════════════════════════

class TestAttributesField(unittest.TestCase):

    def test_attribute_indexed(self):
        meta = extract_cs_metadata(HAS_CACHEABLE_ATTR.encode())
        assert "Cacheable" in meta["attributes"], \
            f"attributes: {meta['attributes']}"

    def test_wrong_attribute_not_indexed(self):
        meta = extract_cs_metadata(HAS_OBSOLETE_NOT_CACHEABLE.encode())
        assert "Cacheable" not in meta["attributes"]

    def test_no_attributes_empty(self):
        meta = extract_cs_metadata(NO_ATTRS.encode())
        assert meta["attributes"] == []

    def test_suffix_stripped(self):
        src = """\
namespace Synth {
    [SerializableAttribute]
    public class Payload { }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "Serializable" in meta["attributes"], \
            f"attributes: {meta['attributes']}"

    def test_attribute_in_string_not_indexed(self):
        src = """\
namespace Synth {
    public class Doc {
        public string Note = \"[Cacheable] attribute description\";
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "Cacheable" not in meta["attributes"]

    def test_attribute_in_comment_not_indexed(self):
        src = """\
namespace Synth {
    // Use [Cacheable] for hot paths
    public class Worker {
        public void DoWork() { }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "Cacheable" not in meta["attributes"]

    def test_multiple_attributes_all_indexed(self):
        src = """\
namespace Synth {
    [Cacheable]
    [Serializable]
    [Obsolete]
    public class Multi { }
}
"""
        meta = extract_cs_metadata(src.encode())
        for attr in ("Cacheable", "Serializable", "Obsolete"):
            assert attr in meta["attributes"], f"'{attr}' missing: {meta['attributes']}"

    def test_method_level_attribute(self):
        src = """\
namespace Synth {
    public class Controller {
        [TestMethod]
        public void MyTest() { }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "TestMethod" in meta["attributes"]

    def test_property_level_attribute(self):
        src = """\
namespace Synth {
    public class Model {
        [Required]
        public string Name { get; set; }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "Required" in meta["attributes"]

    def test_attribute_args_not_in_attributes_list(self):
        """Attribute arguments (like ttl: 60) must not appear as attribute names."""
        meta = extract_cs_metadata(HAS_CACHEABLE_ATTR.encode())
        assert "ttl" not in meta["attributes"]
        assert "60"  not in meta["attributes"]

    def test_attributes_not_in_type_refs(self):
        """Attribute names must not contaminate type_refs."""
        meta = extract_cs_metadata(HAS_CACHEABLE_ATTR.encode())
        assert "Cacheable" not in meta["type_refs"]

    def test_build_document_content_includes_attribute(self):
        """build_document content field has the raw source, so attr name is findable
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
            assert "Cacheable" not in doc.get("attributes", [])
            assert "Cacheable" in doc["content"]
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


# ══════════════════════════════════════════════════════════════════════════════
# Live integration
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestAttrModeLive(LiveTestBase):
    """End-to-end attrs mode: query_by = attributes,filename."""

    @classmethod
    def setUpClass(cls):
        stamp      = int(time.time())
        cls.coll   = f"test_attr_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/ProductRepository.cs": HAS_CACHEABLE_ATTR,
            "synth/LegacyRepository.cs":  HAS_OBSOLETE_NOT_CACHEABLE,
            "synth/PlainRepository.cs":   NO_ATTRS,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, reset=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_finds_annotated_file(self):
        fnames = self._ts_search("Cacheable", "attributes,filename")
        assert "ProductRepository.cs" in fnames

    def test_excludes_differently_decorated_file(self):
        fnames = self._ts_search("Cacheable", "attributes,filename")
        assert "LegacyRepository.cs" not in fnames

    def test_excludes_unannotated_file(self):
        fnames = self._ts_search("Cacheable", "attributes,filename")
        assert "PlainRepository.cs" not in fnames

    def test_obsolete_finds_correct_file(self):
        fnames = self._ts_search("Obsolete", "attributes,filename")
        assert "LegacyRepository.cs"  in fnames
        assert "ProductRepository.cs" not in fnames

    def test_text_mode_broader(self):
        """Text mode would find any file mentioning 'Cacheable' in content."""
        fnames = self._ts_search("Cacheable",
                                 "filename,symbols,class_names,method_names,content")
        assert "ProductRepository.cs" in fnames


if __name__ == "__main__":
    unittest.main()
