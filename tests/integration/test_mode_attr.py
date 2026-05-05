"""
Integration tests for attr mode.

TestAttrModeLive — requires Typesense; tests attr_names field end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    HAS_CACHEABLE_ATTR, HAS_OBSOLETE_NOT_CACHEABLE, NO_ATTRS,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import run_index


class TestAttrModeLive(LiveTestBase):
    """End-to-end attrs mode: query_by = attr_names,filename."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_attr_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/ProductRepository.cs": HAS_CACHEABLE_ATTR,
            "synth/LegacyRepository.cs":  HAS_OBSOLETE_NOT_CACHEABLE,
            "synth/PlainRepository.cs":   NO_ATTRS,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_finds_annotated_file(self):
        fnames = self._ts_search("Cacheable", "attr_names,filename")
        assert "ProductRepository.cs" in fnames

    def test_excludes_differently_decorated_file(self):
        fnames = self._ts_search("Cacheable", "attr_names,filename")
        assert "LegacyRepository.cs" not in fnames

    def test_excludes_unannotated_file(self):
        fnames = self._ts_search("Cacheable", "attr_names,filename")
        assert "PlainRepository.cs" not in fnames

    def test_obsolete_finds_correct_file(self):
        fnames = self._ts_search("Obsolete", "attr_names,filename")
        assert "LegacyRepository.cs"  in fnames
        assert "ProductRepository.cs" not in fnames

    def test_text_mode_broader(self):
        """Text mode would find any file mentioning 'Cacheable' in tokens."""
        fnames = self._ts_search("Cacheable",
                                 "filename,class_names,method_names,tokens")
        assert "ProductRepository.cs" in fnames


if __name__ == "__main__":
    unittest.main()
