"""
Integration tests for implements mode.

TestImplementsModeLive — requires Typesense; tests base_types field end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    IMPLEMENTS_IDATASTORE, USES_IDATASTORE_PARAM, DECLARES_FIELD_IDATASTORE,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


class TestImplementsModeLive(LiveTestBase):
    """End-to-end implements mode: query_by = base_types,class_names,filename."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_impl_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/SqlDataStore.cs": IMPLEMENTS_IDATASTORE,
            "synth/DataTransfer.cs": USES_IDATASTORE_PARAM,
            "synth/CachingProxy.cs": DECLARES_FIELD_IDATASTORE,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
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
