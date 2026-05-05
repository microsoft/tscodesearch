"""
Integration tests for sig / listing modes.

TestSigSearchLive — requires Typesense; tests sig search and pre-filter end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    SIG_HAS_PARAM, FIELD_NAME_ONLY, CALLS_ONLY, NAME_CONTAINS,
    LISTING_TARGET, CALLS_IBLOBSERVICE, CONTENT_ONLY_BLOBSTORE,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


class TestSigSearchLive(LiveTestBase):
    """End-to-end sig search and Bug 2 pre-filter using a real Typesense collection."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp   = int(time.time())
        cls.coll   = f"test_sig_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/DataPipeline.cs":      SIG_HAS_PARAM,
            "synth/DataConsumer.cs":      FIELD_NAME_ONLY,
            "synth/BlobConsumer.cs":      CALLS_ONLY,
            "synth/BlobStoreMigrator.cs": NAME_CONTAINS,
            "synth/Reporter.cs":          CALLS_IBLOBSERVICE,
            "synth/ListingTarget.cs":     LISTING_TARGET,
            "synth/StaticCallOnly.cs":    CONTENT_ONLY_BLOBSTORE,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ── Bug 1: sig search (member_sigs,filename) must not include method_names-only
    def test_sig_finds_param_typed_blobstore(self):
        fnames = self._ts_search("BlobStore", "member_sigs,filename")
        assert "DataPipeline.cs" in fnames, \
            f"Expected DataPipeline.cs; got: {fnames}"

    def test_sig_finds_listing_target(self):
        fnames = self._ts_search("BlobStore", "member_sigs,filename")
        assert "ListingTarget.cs" in fnames

    def test_sig_excludes_calls_only(self):
        """Bug 1 fix: BlobConsumer.cs (calls only) must not appear in sig results."""
        fnames = self._ts_search("BlobStore", "member_sigs,filename")
        assert "BlobConsumer.cs" not in fnames, \
            "calls-only file must not appear in sig search"

    def test_sig_excludes_reporter(self):
        fnames = self._ts_search("BlobStore", "member_sigs,filename")
        assert "Reporter.cs" not in fnames

    def test_old_query_by_would_include_method_names_hits(self):
        """Regression proof: old query_by included method_names — the fixed one has fewer hits."""
        self._ts_search("BlobStore", "member_sigs,method_names,filename")
        new = self._ts_search("BlobStore", "member_sigs,filename")
        # True positives must not be lost
        for fname in ("DataPipeline.cs", "ListingTarget.cs"):
            assert fname in new, f"Fixed search dropped true-positive: {fname}"

    # ── Bug 2: narrow pre-filter must exclude calls-only
    def test_narrow_prefilter_finds_sig_files(self):
        fnames = self._ts_search(
            "BlobStore",
            "member_sigs,class_names,base_types,type_refs,method_names,filename")
        assert "DataPipeline.cs"  in fnames
        assert "ListingTarget.cs" in fnames

    def test_narrow_prefilter_excludes_calls_only(self):
        fnames = self._ts_search(
            "BlobStore",
            "member_sigs,class_names,base_types,type_refs,method_names,filename")
        assert "BlobConsumer.cs" not in fnames, \
            "calls-only file must not appear in narrow pre-filter"
        assert "Reporter.cs"     not in fnames

    def test_broad_prefilter_includes_tokens_only(self):
        """Broad pre-filter (with tokens) includes files where BlobStore
        appears only as a static call target (tokens hit, no declaration)."""
        fnames = self._ts_search(
            "BlobStore",
            "filename,class_names,method_names,tokens")
        assert "StaticCallOnly.cs" in fnames, \
            "Broad pre-filter must include tokens-only file (BlobStore in tokens)"

    def test_narrow_has_fewer_results_than_broad(self):
        narrow = self._ts_search(
            "BlobStore",
            "member_sigs,class_names,base_types,type_refs,method_names,filename",
            per_page=20)
        broad  = self._ts_search(
            "BlobStore",
            "filename,class_names,method_names,tokens",
            per_page=20)
        assert len(narrow) <= len(broad), \
            f"Narrow ({len(narrow)}) must not exceed broad ({len(broad)})"


if __name__ == "__main__":
    unittest.main()
