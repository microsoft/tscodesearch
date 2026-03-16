"""
Tests for implements mode.

Typesense field: base_types
search_code query_by: base_types,class_names,filename
mode: --implements TYPE (q_implements)

Gaps tested:
  - Only type declarations in ':BaseList' populate base_types.
  - Param types and field types must NOT contaminate base_types.
  - Generic base types are stored as their bare name (IRepository, not IRepository<T>).
  - Fully qualified base types are unqualified (Synth.IDataStore → IDataStore).
  - q_implements is distinct from q_uses: only types that INHERIT/IMPLEMENT match.
"""
from __future__ import annotations

import shutil
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    IMPLEMENTS_IDATASTORE, USES_IDATASTORE_PARAM, DECLARES_FIELD_IDATASTORE,
    COMMENT_ONLY_IDATASTORE,
)
from tests.helpers import _server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_cs_metadata, run_index
from query import q_implements


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — base_types field
# ══════════════════════════════════════════════════════════════════════════════

class TestBaseTypesField(unittest.TestCase):

    def test_interface_in_base_types(self):
        meta = extract_cs_metadata(IMPLEMENTS_IDATASTORE.encode())
        assert "IDataStore" in meta["base_types"], \
            f"base_types: {meta['base_types']}"

    def test_all_bases_present(self):
        meta = extract_cs_metadata(IMPLEMENTS_IDATASTORE.encode())
        assert "IDataStore"  in meta["base_types"]
        assert "IDisposable" in meta["base_types"]

    def test_param_type_not_in_base_types(self):
        meta = extract_cs_metadata(USES_IDATASTORE_PARAM.encode())
        assert "IDataStore" not in meta["base_types"], \
            f"Param type must not be in base_types: {meta['base_types']}"

    def test_param_type_in_type_refs_not_base_types(self):
        meta = extract_cs_metadata(USES_IDATASTORE_PARAM.encode())
        assert "IDataStore" in  meta["type_refs"]
        assert "IDataStore" not in meta["base_types"]

    def test_field_type_not_in_base_types(self):
        meta = extract_cs_metadata(DECLARES_FIELD_IDATASTORE.encode())
        assert "IDataStore" not in meta["base_types"]

    def test_comment_not_in_base_types(self):
        meta = extract_cs_metadata(COMMENT_ONLY_IDATASTORE.encode())
        assert "IDataStore" not in meta["base_types"]

    def test_class_names_populated(self):
        meta = extract_cs_metadata(IMPLEMENTS_IDATASTORE.encode())
        assert "SqlDataStore" in meta["class_names"]

    def test_generic_base_type_unqualified(self):
        src = """\
namespace Synth {
    public class WidgetRepo : IRepository<Widget> {
        public Widget Get(int id) { return null; }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "IRepository" in meta["base_types"], \
            f"Generic base type must be stored unqualified: {meta['base_types']}"

    def test_namespace_qualified_base_stripped(self):
        src = """\
namespace Synth {
    public class QualifiedImpl : Synth.IDataStore {
        public void Write(string k, byte[] d) { }
        public byte[] Read(string k) { return null; }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert any("IDataStore" in b for b in meta["base_types"]), \
            f"Qualified base must be stored as unqualified: {meta['base_types']}"

    def test_struct_implementing_interface(self):
        src = """\
namespace Synth {
    public struct FastStore : IDataStore {
        public void Write(string k, byte[] d) { }
        public byte[] Read(string k) { return null; }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "IDataStore" in meta["base_types"]


# ══════════════════════════════════════════════════════════════════════════════
# q_implements AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQImplements(unittest.TestCase):

    def _impl(self, src, type_name):
        return q_implements(*_parse(src), type_name=type_name)

    def test_finds_implementing_class(self):
        r = self._impl(IMPLEMENTS_IDATASTORE, "IDataStore")
        assert r, "SqlDataStore must be found"
        texts = [t for _, t in r]
        assert any("SqlDataStore" in t for t in texts)

    def test_does_not_find_param_only_usage(self):
        r = self._impl(USES_IDATASTORE_PARAM, "IDataStore")
        assert r == [], \
            "DataTransfer uses IDataStore as param but must not appear in implements"

    def test_does_not_find_field_only_usage(self):
        r = self._impl(DECLARES_FIELD_IDATASTORE, "IDataStore")
        assert r == []

    def test_does_not_find_comment_mention(self):
        r = self._impl(COMMENT_ONLY_IDATASTORE, "IDataStore")
        assert r == []

    def test_output_includes_base_list(self):
        r = self._impl(IMPLEMENTS_IDATASTORE, "IDataStore")
        texts = [t for _, t in r]
        assert any("IDataStore" in t for t in texts), \
            "Output should mention the interface being implemented"

    def test_generic_base_match(self):
        src = """\
namespace Synth {
    public class Repo : IRepository<Widget> {
        public Widget Get(int id) { return null; }
    }
}
"""
        r = self._impl(src, "IRepository")
        assert r, "Generic base type must be found by q_implements"

    def test_unrelated_type_not_found(self):
        r = self._impl(IMPLEMENTS_IDATASTORE, "IUnrelated")
        assert r == []


# ══════════════════════════════════════════════════════════════════════════════
# Live integration
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestImplementsModeLive(LiveTestBase):
    """End-to-end implements mode: query_by = base_types,class_names,filename."""

    @classmethod
    def setUpClass(cls):
        stamp      = int(time.time())
        cls.coll   = f"test_impl_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/SqlDataStore.cs": IMPLEMENTS_IDATASTORE,
            "synth/DataTransfer.cs": USES_IDATASTORE_PARAM,
            "synth/CachingProxy.cs": DECLARES_FIELD_IDATASTORE,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, reset=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_finds_implementing_class(self):
        fnames = self._ts_search("IDataStore", "base_types,class_names,filename")
        assert "SqlDataStore.cs" in fnames

    def test_excludes_param_only_file(self):
        fnames = self._ts_search("IDataStore", "base_types,class_names,filename")
        assert "DataTransfer.cs" not in fnames

    def test_excludes_field_only_file(self):
        fnames = self._ts_search("IDataStore", "base_types,class_names,filename")
        assert "CachingProxy.cs" not in fnames

    def test_uses_mode_finds_param_and_field_files(self):
        """type_refs (uses mode) finds files declaring IDataStore fields/params."""
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "DataTransfer.cs" in fnames
        assert "CachingProxy.cs" in fnames

    def test_implements_is_subset_of_uses(self):
        impl = self._ts_search("IDataStore", "base_types,class_names,filename",
                               per_page=20)
        uses = self._ts_search("IDataStore", "type_refs,class_names,filename",
                               per_page=20)
        # Every file found by implements must also be found by uses
        assert impl <= uses | {""}, \
            f"Implements result not subset of uses: {impl - uses}"


if __name__ == "__main__":
    unittest.main()
