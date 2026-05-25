"""
Integration tests for the indexer: collection creation, file indexing, and field discrimination.

TestIndexer                  -- collection creation, file count, paths, priority, reset
TestSemanticFieldDiscrim     -- each per-identifier field returns ONLY the file where the
                               identifier appears in that exact role (param vs field vs
                               base vs local vs cast vs call vs string-literal)
TestMultiRoot                -- two independent collections from the same source tree
TestSearchFieldModes         -- each MCP search mode's query_by field returns the right file

All classes open a real Tantivy index inline -- no daemon required.
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
from query.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()

# -- TestIndexer ---------------------------------------------------------------

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
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
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
        hits = _search(self.coll, "deploy", query_by="path_tokens,tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("deploy.py", names, f"deploy.py not in {names}")

    def test_markdown_indexed(self):
        hits = _search(self.coll, "project", query_by="path_tokens,tokens")
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

    def test_path_segments_extracted(self):
        hits = _search(self.coll, "BlobStore")
        blob = next((h for h in hits if h["filename"] == "BlobStore.cs"), None)
        self.assertIsNotNone(blob, "BlobStore.cs not found")
        # path_segments contains every ancestor folder for path-prefix filtering.
        # "storage/BlobStore.cs" has only "storage" (the file's own basename is excluded).
        self.assertIn("storage", blob.get("path_segments", []))

    def test_reset_recreates_collection(self):
        """resethard=True drops and recreates the collection."""
        old_info = _collection_info(self.coll)
        self.assertIsNotNone(old_info)
        run_index(_cfg, src_root=self.tmpdir, collection=self.coll, resethard=True, verbose=False)
        new_info = _collection_info(self.coll)
        self.assertIsNotNone(new_info)
        # Doc count should match between old and new; resethard doesn't change file set.
        self.assertEqual(old_info["num_documents"], new_info["num_documents"])


# -- TestSemanticFieldDiscrim -------------------------------------------------

# Fixtures designed so the identifier ``IDisposable`` appears in exactly one
# structural role per file. Each search-by-field test then asserts that only
# the file holding ``IDisposable`` in that role comes back -- verifying both
# that the indexer wrote the right field AND that searching that field is
# discriminating. This is a meaningful end-to-end check; just round-tripping
# extract_metadata through the file system would only re-test the parser.

_BASE_IMPLEMENTOR_CS = """\
using System;
namespace Discrim {
    [Serializable]
    public class BaseImplementor : IDisposable {
        public void DoWork() { }
        public void Dispose() { }
    }
}
"""

_PARAM_USER_CS = """\
namespace Discrim {
    public class ParamUser {
        public void Accept(IDisposable d) { }
    }
}
"""

_FIELD_OWNER_CS = """\
namespace Discrim {
    public class FieldOwner {
        private IDisposable _disposable;
    }
}
"""

_RETURN_USER_CS = """\
namespace Discrim {
    public class ReturnUser {
        public IDisposable Create() { return null; }
    }
}
"""

_LOCAL_USER_CS = """\
namespace Discrim {
    public class LocalUser {
        public void Run() {
            IDisposable d = null;
        }
    }
}
"""

_CAST_USER_CS = """\
namespace Discrim {
    public class CastUser {
        public void Run(object o) { var d = (IDisposable)o; }
    }
}
"""

_CALLER_CS = """\
namespace Discrim {
    public class Caller {
        public void Run(BaseImplementor impl) { impl.DoWork(); }
    }
}
"""

_STRING_MENTION_CS = """\
namespace Discrim {
    public class StringMention {
        public string Description = "documents IDisposable cleanup";
    }
}
"""

_UNRELATED_CS = """\
namespace Discrim {
    public class Unrelated {
        public string Greeting = "hello";
    }
}
"""


class TestSemanticFieldDiscrim(unittest.TestCase):
    """Field-by-field discrimination: search by exactly one field for a single
    identifier and assert which file comes back. No re-parsing on the test side
    -- every assertion is end-to-end through the Tantivy index."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_discrim_{stamp}"
        cls.tmpdir = _make_git_repo({
            "BaseImplementor.cs": _BASE_IMPLEMENTOR_CS,
            "ParamUser.cs":       _PARAM_USER_CS,
            "FieldOwner.cs":      _FIELD_OWNER_CS,
            "ReturnUser.cs":      _RETURN_USER_CS,
            "LocalUser.cs":       _LOCAL_USER_CS,
            "CastUser.cs":        _CAST_USER_CS,
            "Caller.cs":          _CALLER_CS,
            "StringMention.cs":   _STRING_MENTION_CS,
            "Unrelated.cs":       _UNRELATED_CS,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _files(self, q: str, query_by: str) -> set:
        hits = _search(self.coll, q, query_by=query_by, per_page=20)
        return {h["filename"] for h in hits}

    # -- single-field discrimination on ``IDisposable`` ------------------------

    def test_base_types_finds_only_implementor(self):
        self.assertEqual(self._files("IDisposable", "base_types"),
                         {"BaseImplementor.cs"})

    def test_param_types_finds_only_param_user(self):
        self.assertEqual(self._files("IDisposable", "param_types"),
                         {"ParamUser.cs"})

    def test_field_types_finds_only_field_owner(self):
        self.assertEqual(self._files("IDisposable", "field_types"),
                         {"FieldOwner.cs"})

    def test_return_types_finds_only_return_user(self):
        self.assertEqual(self._files("IDisposable", "return_types"),
                         {"ReturnUser.cs"})

    def test_local_types_finds_only_local_user(self):
        self.assertEqual(self._files("IDisposable", "local_types"),
                         {"LocalUser.cs"})

    def test_cast_types_finds_only_cast_user(self):
        self.assertEqual(self._files("IDisposable", "cast_types"),
                         {"CastUser.cs"})

    # -- type_refs is the union of typed-use roles (not cast_types) ------------

    def test_type_refs_unions_typed_use_roles(self):
        self.assertEqual(
            self._files("IDisposable", "type_refs"),
            {"BaseImplementor.cs", "ParamUser.cs", "FieldOwner.cs",
             "ReturnUser.cs", "LocalUser.cs"},
        )

    # -- string-literal mentions must not leak into structured fields ----------

    def test_string_literal_excluded_from_every_structured_field(self):
        for field in ("base_types", "param_types", "field_types",
                      "return_types", "local_types", "cast_types",
                      "type_refs", "method_names", "class_names",
                      "attr_names", "tokens"):
            self.assertNotIn(
                "StringMention.cs", self._files("IDisposable", field),
                f"StringMention.cs leaked into {field} via string literal",
            )

    # -- method_names: only the declarer ---------------------------------------

    def test_method_names_finds_only_declarer(self):
        # Caller.cs CALLS DoWork(); only BaseImplementor.cs DECLARES it.
        self.assertEqual(self._files("DoWork", "method_names"),
                         {"BaseImplementor.cs"})

    # -- call_sites: only the caller, not the declarer -------------------------

    def test_call_sites_finds_only_caller(self):
        self.assertEqual(self._files("DoWork", "call_sites"),
                         {"Caller.cs"})

    # -- attr_names: only attribute decorations --------------------------------

    def test_attr_names_finds_only_decorated_file(self):
        self.assertEqual(self._files("Serializable", "attr_names"),
                         {"BaseImplementor.cs"})

    # -- class_names: only declarations, not usages ----------------------------

    def test_class_names_finds_only_declarer(self):
        # Caller.cs uses BaseImplementor as a parameter type -- it must NOT
        # appear in class_names search for BaseImplementor.
        self.assertEqual(self._files("BaseImplementor", "class_names"),
                         {"BaseImplementor.cs"})

    # -- imports: only the file that imports the namespace --------------------

    def test_imports_finds_only_importer(self):
        self.assertEqual(self._files("System", "imports"),
                         {"BaseImplementor.cs"})

    # -- namespace: every file in this fixture shares ``Discrim`` --------------

    def test_namespace_split_into_components(self):
        # All fixture files declare ``namespace Discrim``; the indexer stores
        # it as the single component "Discrim", searchable by exact name.
        fnames = self._files("Discrim", "namespace")
        self.assertIn("BaseImplementor.cs", fnames)
        self.assertEqual(len(fnames), 9,
            f"every fixture file should match: {fnames}")


# -- TestMultiRoot -------------------------------------------------------------

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
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll_a, resethard=True, verbose=False)
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll_b, resethard=True, verbose=False)
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


# -- TestSearchFieldModes ------------------------------------------------------

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
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _qby(self, q, query_by, per_page=10):
        return _search(self.coll, q, query_by=query_by, per_page=per_page)

    def test_implements_mode_base_types(self):
        hits = self._qby("IDisposable", "base_types,class_names,path_tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_calls_mode_call_sites(self):
        hits = self._qby("DoWork", "call_sites,path_tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("Bar.cs", names)

    def test_declarations_mode_method_names(self):
        # ``method_names`` is the modern equivalent of the old member_sigs
        # field (the daemon's declarations resolver queries method_names).
        hits = self._qby("Dispose", "method_names,class_names,path_tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_uses_mode_type_refs(self):
        hits = self._qby("Foo", "type_refs,class_names,path_tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("Bar.cs", names)

    def test_attrs_mode_attr_names(self):
        hits = self._qby("Serializable", "attr_names,path_tokens")
        names = [h["filename"] for h in hits]
        self.assertIn("Foo.cs", names)

    def test_namespace_in_query(self):
        hits = self._qby("TestNs", "namespace,tokens,path_tokens")
        self.assertGreater(len(hits), 0)


if __name__ == "__main__":
    unittest.main()
