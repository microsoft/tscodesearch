"""
Integration tests for Python support: Typesense indexing of Python semantic fields.

TestPySemanticFields — requires Typesense to be running.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _assert_server_ok, _search, _delete_collection, _make_git_repo,
    _FOO_PY, _BAR_PY,
)
from indexserver.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


class TestPySemanticFields(unittest.TestCase):
    """Verify that Python files get their semantic fields indexed by Typesense."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_pysem_{stamp}"
        cls.tmpdir = _make_git_repo({
            "myapp/foo.py": _FOO_PY,
            "myapp/bar.py": _BAR_PY,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, filename):
        hits = _search(self.coll, os.path.splitext(filename)[0],
                       query_by="filename,class_names,method_names,tokens")
        return next((h for h in hits if h["filename"] == filename), None)

    def test_py_class_names_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("Foo", foo.get("class_names", []))

    def test_py_method_names_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("process", foo.get("method_names", []))

    def test_py_base_types_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("IFoo", foo.get("base_types", []))

    def test_py_subclass_base_types_indexed(self):
        bar = self._get("bar.py")
        self.assertIsNotNone(bar)
        self.assertIn("Foo", bar.get("base_types", []))

    def test_py_call_sites_indexed(self):
        bar = self._get("bar.py")
        self.assertIsNotNone(bar)
        self.assertIn("process", bar.get("call_sites", []))

    def test_py_decorators_in_attr_names(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("dataclass", foo.get("attr_names", []))

    def test_py_imports_in_usings(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("os", foo.get("usings", []))

    def test_py_base_types_searchable_via_typesense(self):
        hits = _search(self.coll, "IFoo", query_by="base_types,class_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("foo.py", names)

    def test_py_call_sites_searchable_via_typesense(self):
        hits = _search(self.coll, "process", query_by="call_sites,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("bar.py", names)

    def test_py_member_sigs_searchable_via_typesense(self):
        hits = _search(self.coll, "process", query_by="member_sigs,method_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("foo.py", names)


if __name__ == "__main__":
    unittest.main()
