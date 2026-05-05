"""
Integration tests for uses mode.

TestUsesModeLive — requires Typesense; tests type_refs field end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    DECLARES_FIELD_IDATASTORE, USES_IDATASTORE_PARAM, COMMENT_ONLY_IDATASTORE,
    IMPLEMENTS_IDATASTORE,
    LOCAL_VAR_IDATASTORE, STATIC_RECEIVER_IDATASTORE,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import run_index


class TestUsesModeLive(LiveTestBase):
    """End-to-end uses mode: query_by = type_refs,class_names,filename."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_uses_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/SqlDataStore.cs":  IMPLEMENTS_IDATASTORE,
            "synth/DataTransfer.cs":  USES_IDATASTORE_PARAM,
            "synth/CachingProxy.cs":  DECLARES_FIELD_IDATASTORE,
            "synth/Indirect.cs":      COMMENT_ONLY_IDATASTORE,
            "synth/LocalVarUser.cs":  LOCAL_VAR_IDATASTORE,
            "synth/StaticUser.cs":    STATIC_RECEIVER_IDATASTORE,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_finds_param_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "DataTransfer.cs" in fnames

    def test_finds_field_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "CachingProxy.cs" in fnames

    def test_excludes_comment_only_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "Indirect.cs" not in fnames

    def test_text_mode_finds_comment_file(self):
        """Tokens field picks up comments — text mode is broader than uses mode."""
        fnames = self._ts_search("IDataStore",
                                 "filename,class_names,method_names,tokens")
        assert "Indirect.cs" in fnames

    def test_finds_local_var_file(self):
        """File where IDataStore appears only as a local variable type must be found."""
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "LocalVarUser.cs" in fnames, \
            f"Local-var-only file must be found by uses mode: {fnames}"

    def test_finds_static_receiver_file(self):
        """File where IDataStore appears only as a static call receiver must be found."""
        fnames = self._ts_search("IDataStore", "type_refs,class_names,filename")
        assert "StaticUser.cs" in fnames, \
            f"Static-receiver file must be found by uses mode: {fnames}"

    def test_uses_finds_more_than_implements(self):
        uses = self._ts_search("IDataStore", "type_refs,class_names,filename",
                               per_page=20)
        impl = self._ts_search("IDataStore", "base_types,class_names,filename",
                               per_page=20)
        assert len(uses) >= len(impl), \
            "uses mode must return >= files compared to implements mode"


if __name__ == "__main__":
    unittest.main()
