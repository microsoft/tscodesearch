"""
Integration tests for casts mode.

TestCastTypesLive — requires Typesense; tests cast_types field end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    CASTS_TO_BLOBSTORE, USES_BLOBSTORE_NO_CAST, CAST_IN_CONDITIONAL,
    AS_CAST_ONLY_BLOBSTORE,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


class TestCastTypesLive(LiveTestBase):
    """End-to-end: cast_types field enables Typesense pre-filter for cast sites."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_casts_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/Downcast.cs":    CASTS_TO_BLOBSTORE,
            "synth/NoCast.cs":      USES_BLOBSTORE_NO_CAST,
            "synth/AsCastOnly.cs":  AS_CAST_ONLY_BLOBSTORE,
            "synth/CondCast.cs":    CAST_IN_CONDITIONAL,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_explicit_cast_file_found_via_cast_types(self):
        fnames = self._ts_search("BlobStore", "cast_types,filename")
        assert "Downcast.cs" in fnames, \
            f"File with explicit cast must be found via cast_types: {fnames}"

    def test_conditional_cast_file_found(self):
        fnames = self._ts_search("BlobStore", "cast_types,filename")
        assert "CondCast.cs" in fnames, \
            f"File with cast in conditional must be found: {fnames}"

    def test_no_cast_file_excluded(self):
        fnames = self._ts_search("BlobStore", "cast_types,filename")
        assert "NoCast.cs" not in fnames, \
            "File with no explicit casts must not appear in cast_types results"

    def test_as_cast_file_excluded(self):
        """as-casts must not populate cast_types, so AsCastOnly must not be found."""
        fnames = self._ts_search("BlobStore", "cast_types,filename")
        assert "AsCastOnly.cs" not in fnames, \
            "as-cast file must not appear in cast_types search"

    def test_cast_types_narrower_than_uses(self):
        """cast_types must return a strict subset of type_refs results."""
        casts = self._ts_search("BlobStore", "cast_types,filename", per_page=20)
        uses  = self._ts_search("BlobStore", "type_refs,class_names,filename",
                                per_page=20)
        assert casts <= uses, \
            f"cast_types results must be subset of uses results: {casts - uses}"


if __name__ == "__main__":
    unittest.main()
