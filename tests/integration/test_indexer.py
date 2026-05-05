"""
Integration tests for the indexer: collection creation, file indexing, and semantic fields.

TestIndexer          — collection creation, file count, paths, priority, reset
TestSemanticFields   — all indexed fields: base_types, call_sites, member_sigs, etc.
TestMultiRoot        — two independent collections from the same source tree
TestSearchFieldModes — each MCP search mode's query_by field returns the right file

All classes require Typesense to be running.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _assert_server_ok, _search, _collection_info, _delete_collection, _make_git_repo,
    _FOO_CS, _BAR_CS, _BLOBSTORE_CS,
)
from indexserver.indexer import run_index


# ── TestIndexer ───────────────────────────────────────────────────────────────

class TestIndexer(unittest.TestCase):
    """Indexer creates a collection and indexes C# + other files."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_idx_{stamp}"
        cls.tmpdir = _make_git_repo({
            "myapp/Foo.cs":          _FOO_CS,
            "myapp/Bar.cs":          _BAR_CS,
            "storage/BlobStore.cs":  _BLOBSTORE_CS,
            "scripts/deploy.py":     "# deployment script\ndef run(): pass\n",
            "README.md":             "# My project\n",
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_collection_created(self):
        info = _collection_info(self.coll)
        self.assertIsNotNone(info, f"Collection {self.coll!r} not found")

    def test_all_files_indexed(self):
        info = _collection_info(self.coll)
        self.assertGreaterEqual(info["num_documents"], 5,
            f"Expected >=5 docs, got {info['num_documents']}")

    def test_cs_file_findable(self):
        hits = _search(self.coll, "Foo")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names, f"Foo.cs not in {names}")

    def test_python_file_indexed(self):
        hits = _search(self.coll, "deploy", query_by="filename,tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("deploy.py", names, f"deploy.py not in {names}")

    def test_markdown_indexed(self):
        hits = _search(self.coll, "project", query_by="filename,tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("README.md", names, f"README.md not in {names}")

    def test_relative_path_not_absolute(self):
        hits = _search(self.coll, "Foo")
        tmpdir_norm = self.tmpdir.replace("\\", "/").lower()
        for h in hits:
            self.assertNotIn(tmpdir_norm, h["relative_path"].lower(),
                f"relative_path contains tmpdir: {h['relative_path']}")

    def test_relative_path_structure(self):
        hits = _search(self.coll, "Foo")
        foo = next((h for h in hits if h["filename"] == "Foo.cs"), None)
        self.assertIsNotNone(foo, "Foo.cs not found")
        self.assertEqual(foo["relative_path"], "myapp/Foo.cs",
            f"Expected myapp/Foo.cs, got {foo['relative_path']}")

    def test_subsystem_extracted(self):
        hits = _search(self.coll, "BlobStore")
        blob = next((h for h in hits if h["filename"] == "BlobStore.cs"), None)
        self.assertIsNotNone(blob, "BlobStore.cs not found")
        self.assertEqual(blob["subsystem"], "storage")

    def test_reset_recreates_collection(self):
        """resethard=True drops and recreates the collection."""
        old_info = _collection_info(self.coll)
        time.sleep(1.1)
        run_index(src_root=self.tmpdir, collection=self.coll, resethard=True, verbose=False)
        time.sleep(0.3)
        new_info = _collection_info(self.coll)
        self.assertIsNotNone(new_info)
        self.assertNotEqual(old_info.get("created_at"), new_info.get("created_at"),
            "Collection was not recreated (same created_at)")


# ── TestSemanticFields ────────────────────────────────────────────────────────

class TestSemanticFields(unittest.TestCase):
    """tree-sitter extracts the right symbols and semantic metadata."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_sem_{stamp}"
        cls.tmpdir = _make_git_repo({
            "core/Foo.cs": _FOO_CS,
            "core/Bar.cs": _BAR_CS,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, filename):
        base = os.path.splitext(filename)[0]
        hits = _search(self.coll, base, per_page=5)
        return next((h for h in hits if h["filename"] == filename), None)

    def test_base_types_interface(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertIn("IDisposable", foo.get("base_types", []),
            f"base_types: {foo.get('base_types')}")

    def test_base_types_multiple(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertIn("IComparable", foo.get("base_types", []),
            f"base_types: {foo.get('base_types')}")

    def test_base_class_in_base_types(self):
        bar = self._get("Bar.cs")
        self.assertIsNotNone(bar)
        self.assertIn("Foo", bar.get("base_types", []),
            f"base_types for Bar: {bar.get('base_types')}")

    def test_call_sites(self):
        bar = self._get("Bar.cs")
        self.assertIsNotNone(bar)
        self.assertIn("DoWork", bar.get("call_sites", []),
            f"call_sites: {bar.get('call_sites')}")

    def test_type_refs(self):
        bar = self._get("Bar.cs")
        self.assertIsNotNone(bar)
        self.assertIn("Foo", bar.get("type_refs", []),
            f"type_refs: {bar.get('type_refs')}")

    def test_attr_names(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertIn("Serializable", foo.get("attr_names", []),
            f"attr_names: {foo.get('attr_names')}")

    def test_usings(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertIn("System", foo.get("usings", []),
            f"usings: {foo.get('usings')}")

    def test_class_names(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertIn("Foo", foo.get("class_names", []))

    def test_method_names(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        methods = foo.get("method_names", [])
        self.assertIn("Dispose", methods, f"method_names: {methods}")
        self.assertIn("DoWork",  methods, f"method_names: {methods}")

    def test_member_sigs(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        sigs = foo.get("member_sigs", [])
        self.assertTrue(any("Dispose" in s for s in sigs),
                        f"expected 'Dispose' in member_sigs: {sigs}")
        self.assertTrue(any("DoWork" in s for s in sigs),
                        f"expected 'DoWork' in member_sigs: {sigs}")

    def test_namespace(self):
        foo = self._get("Foo.cs")
        self.assertIsNotNone(foo)
        self.assertEqual(foo.get("namespace"), "TestNs",
                         f"namespace: {foo.get('namespace')}")


# ── TestMultiRoot ─────────────────────────────────────────────────────────────

class TestMultiRoot(unittest.TestCase):
    """Two independent collections for the same source tree stay isolated."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll_a = f"test_root_a_{stamp}"
        cls.coll_b = f"test_root_b_{stamp}"
        cls.tmpdir = _make_git_repo({
            "Foo.cs": _FOO_CS,
            "Bar.cs": _BAR_CS,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll_a, resethard=True, verbose=False)
        run_index(src_root=cls.tmpdir, collection=cls.coll_b, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll_a)
        _delete_collection(cls.coll_b)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_both_collections_exist(self):
        self.assertIsNotNone(_collection_info(self.coll_a))
        self.assertIsNotNone(_collection_info(self.coll_b))

    def test_coll_a_searchable(self):
        hits = _search(self.coll_a, "Foo")
        self.assertGreater(len(hits), 0)

    def test_coll_b_searchable(self):
        hits = _search(self.coll_b, "Foo")
        self.assertGreater(len(hits), 0)

    def test_same_doc_count(self):
        a = _collection_info(self.coll_a)["num_documents"]
        b = _collection_info(self.coll_b)["num_documents"]
        self.assertEqual(a, b, f"coll_a={a} docs vs coll_b={b} docs")

    def test_nonexistent_collection_returns_none(self):
        self.assertIsNone(_collection_info("codesearch_does_not_exist_xyz"))


# ── TestSearchFieldModes ──────────────────────────────────────────────────────

class TestSearchFieldModes(unittest.TestCase):
    """Each MCP search mode's query_by field string returns the right file."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_modes_{stamp}"
        cls.tmpdir = _make_git_repo({
            "core/Foo.cs": _FOO_CS,
            "core/Bar.cs": _BAR_CS,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _qby(self, q, query_by, per_page=10):
        return _search(self.coll, q, query_by=query_by, per_page=per_page)

    def test_implements_mode_base_types(self):
        hits = self._qby("IDisposable", "base_types,class_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_calls_mode_call_sites(self):
        hits = self._qby("DoWork", "call_sites,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("Bar.cs", names)

    def test_declarations_mode_member_sigs(self):
        hits = self._qby("Dispose", "member_sigs,method_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_uses_mode_type_refs(self):
        hits = self._qby("Foo", "type_refs,class_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("Bar.cs", names)

    def test_attrs_mode_attr_names(self):
        hits = self._qby("Serializable", "attr_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_namespace_in_query(self):
        hits = self._qby("TestNs", "tokens,filename")
        self.assertGreater(len(hits), 0)


if __name__ == "__main__":
    unittest.main()
