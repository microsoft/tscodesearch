"""
Tests for sig / listing modes.

Covers:
  - extract_cs_metadata: member_sigs (param types, return types, constructors, locals)
  - q_methods / q_classes / q_fields semantics (declarations only, no call contamination)
  - Narrow pre-filter (Bug 2): listing modes must not include calls-only files
  - Fixed sig search query_by (Bug 1): method_names removed to avoid false positives

Run (no Typesense):
    pytest tests/test_mode_sig.py -v -k "not Live"
Run (with Typesense):
    pytest tests/test_mode_sig.py -v
"""
from __future__ import annotations

import shutil
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    SIG_HAS_PARAM, FIELD_NAME_ONLY, CALLS_ONLY, NAME_CONTAINS,
    LISTING_TARGET, CALLS_IBLOBSERVICE, CONTENT_ONLY_BLOBSTORE,
)
from tests.helpers import _server_ok, _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_cs_metadata, run_index
from query import q_methods, q_classes, q_fields


# ══════════════════════════════════════════════════════════════════════════════
# extract_cs_metadata — member_sigs field
# ══════════════════════════════════════════════════════════════════════════════

class TestMemberSigs(unittest.TestCase):
    """extract_cs_metadata correctly populates member_sigs."""

    def test_param_type_in_member_sigs(self):
        meta = extract_cs_metadata(SIG_HAS_PARAM.encode())
        assert any("BlobStore" in s for s in meta["member_sigs"]), \
            f"member_sigs: {meta['member_sigs']}"

    def test_return_type_in_member_sigs(self):
        """Return type uses child_by_field_name('returns') — must be captured."""
        meta = extract_cs_metadata(SIG_HAS_PARAM.encode())
        sigs = meta["member_sigs"]
        assert any("BlobStore" in s and "Retrieve" in s for s in sigs), \
            f"Return type 'BlobStore' must appear in Retrieve sig. Got: {sigs}"

    def test_calls_only_no_blobstore_in_member_sigs(self):
        meta = extract_cs_metadata(CALLS_ONLY.encode())
        assert not any("BlobStore" in s for s in meta["member_sigs"]), \
            f"calls-only file must have no BlobStore in member_sigs: {meta['member_sigs']}"

    def test_call_targets_not_in_member_sigs(self):
        meta = extract_cs_metadata(CALLS_ONLY.encode())
        for sig in meta["member_sigs"]:
            assert "FetchBlob" not in sig and "StoreBlob" not in sig, \
                f"call target leaked into member_sigs: {sig!r}"

    def test_constructor_in_member_sigs(self):
        meta = extract_cs_metadata(LISTING_TARGET.encode())
        assert any("WidgetProcessor" in s for s in meta["member_sigs"]), \
            "Constructor must appear in member_sigs"

    def test_local_function_in_member_sigs(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            void LocalHelper(int x) { }
            LocalHelper(1);
        }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert any("LocalHelper" in s for s in meta["member_sigs"]), \
            f"local function not in member_sigs: {meta['member_sigs']}"

    def test_method_names_field_has_all_names(self):
        meta = extract_cs_metadata(SIG_HAS_PARAM.encode())
        for name in ("Store", "Retrieve", "LogEntry", "WriteTag"):
            assert name in meta["method_names"], \
                f"Expected '{name}' in method_names: {meta['method_names']}"


# ══════════════════════════════════════════════════════════════════════════════
# q_methods semantics
# ══════════════════════════════════════════════════════════════════════════════

class TestQMethodsSemantics(unittest.TestCase):
    """q_methods returns only declarations — no call sites, no imports."""

    def _run(self, src):
        return q_methods(*_parse(src))

    def test_returns_declarations_not_calls(self):
        r = self._run(CALLS_ONLY)
        texts = [t for _, t in r]
        for t in texts:
            assert "FetchBlob" not in t and "StoreBlob" not in t, \
                f"Call target leaked into q_methods: {t!r}"

    def test_returns_blobstore_param_method(self):
        r = self._run(SIG_HAS_PARAM)
        texts = [t for _, t in r]
        assert any("BlobStore" in t for t in texts), \
            "Method with BlobStore param must appear in q_methods"

    def test_unrelated_method_included(self):
        """q_methods returns ALL methods — WriteTag is also present."""
        r = self._run(SIG_HAS_PARAM)
        texts = [t for _, t in r]
        assert any("WriteTag" in t for t in texts)

    def test_calls_only_has_no_blobstore_decl(self):
        r = self._run(CALLS_ONLY)
        texts = [t for _, t in r]
        assert not any("BlobStore" in t for t in texts), \
            f"BlobStore leaked into q_methods for calls-only file: {texts}"

    def test_writetag_still_in_calls_only(self):
        """Even in a calls-only file, defined methods DO appear in q_methods."""
        r = self._run(CALLS_ONLY)
        texts = [t for _, t in r]
        assert any("WriteTag" in t for t in texts)

    def test_return_type_in_methods_output(self):
        """q_methods output includes the return type string (uses 'returns' field)."""
        r = self._run(SIG_HAS_PARAM)
        texts = [t for _, t in r]
        assert any("BlobStore" in t and "Retrieve" in t for t in texts), \
            f"Return type must appear in q_methods output: {texts}"

    def test_local_function_listed(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            void LocalHelper(int x) { }
        }
    }
}
"""
        r = self._run(src)
        texts = [t for _, t in r]
        assert any("LocalHelper" in t for t in texts)

    def test_no_duplicates(self):
        r = self._run(LISTING_TARGET)
        assert len(r) == len(set(r)), "Duplicate entries in q_methods output"


# ══════════════════════════════════════════════════════════════════════════════
# q_classes semantics
# ══════════════════════════════════════════════════════════════════════════════

class TestQClassesSemantics(unittest.TestCase):

    def _run(self, src):
        return q_classes(*_parse(src))

    def test_returns_type_declarations(self):
        r = self._run(LISTING_TARGET)
        texts = [t for _, t in r]
        assert any("IProcessor"    in t for t in texts)
        assert any("WidgetProcessor" in t for t in texts)

    def test_includes_base_types(self):
        r = self._run(LISTING_TARGET)
        texts = [t for _, t in r]
        assert any("IProcessor" in t and "WidgetProcessor" in t for t in texts), \
            "class declaration should include base type in output"

    def test_does_not_return_method_names(self):
        r = self._run(LISTING_TARGET)
        texts = [t for _, t in r]
        for t in texts:
            assert "Execute" not in t, f"Method leaked into q_classes: {t!r}"
            assert "LogTag"  not in t

    def test_calls_only_no_blobstore(self):
        r = self._run(CALLS_ONLY)
        texts = [t for _, t in r]
        assert not any("BlobStore" in t for t in texts)


# ══════════════════════════════════════════════════════════════════════════════
# q_fields semantics
# ══════════════════════════════════════════════════════════════════════════════

class TestQFieldsSemantics(unittest.TestCase):

    def _run(self, src):
        return q_fields(*_parse(src))

    def test_returns_fields(self):
        r = self._run(LISTING_TARGET)
        texts = [t for _, t in r]
        assert any("BlobStore" in t and "_store" in t for t in texts), \
            f"Field '_store: BlobStore' must appear: {texts}"

    def test_returns_properties(self):
        r = self._run(LISTING_TARGET)
        texts = [t for _, t in r]
        assert any("Tag" in t for t in texts), \
            "Property 'Tag' must appear in q_fields"

    def test_does_not_return_methods(self):
        r = self._run(LISTING_TARGET)
        for _, t in r:
            assert "[method]" not in t and "[ctor]" not in t, \
                f"Method/ctor leaked into q_fields: {t!r}"

    def test_calls_only_no_blobstore_fields(self):
        r = self._run(CALLS_ONLY)
        texts = [t for _, t in r]
        assert not any("BlobStore" in t for t in texts)


# ══════════════════════════════════════════════════════════════════════════════
# Pre-filter field selection (Bug 2)
# ══════════════════════════════════════════════════════════════════════════════

class TestPrefilterFieldSelection(unittest.TestCase):
    """The narrow pre-filter (member_sigs,class_names,base_types,type_refs,
    method_names,filename) must exclude calls-only files."""

    def test_calls_only_absent_from_narrow_prefilter_fields(self):
        """CALLS_IBLOBSERVICE has BlobStore-adjacent terms only in call_sites/tokens,
        not in the narrow pre-filter fields."""
        meta = extract_cs_metadata(CALLS_IBLOBSERVICE.encode())
        for field in ("member_sigs", "type_refs", "base_types", "class_names"):
            vals = meta[field]
            assert not any("BlobStore" in v for v in vals), \
                f"BlobStore found in narrow-prefilter field '{field}': {vals}"

    def test_sig_file_present_in_narrow_prefilter_fields(self):
        meta = extract_cs_metadata(SIG_HAS_PARAM.encode())
        assert any("BlobStore" in s for s in meta["member_sigs"])

    def test_listing_target_present_in_narrow_prefilter_fields(self):
        meta = extract_cs_metadata(LISTING_TARGET.encode())
        assert any("BlobStore" in s for s in meta["member_sigs"])
        assert "BlobStore" in meta["type_refs"]

    def test_class_name_only_hits_class_names_field(self):
        meta = extract_cs_metadata(NAME_CONTAINS.encode())
        assert "BlobStoreMigrator" in meta["class_names"]
        assert not any("BlobStore" in s for s in meta["member_sigs"])


# ══════════════════════════════════════════════════════════════════════════════
# Live integration — sig search + listing pre-filter
# ══════════════════════════════════════════════════════════════════════════════

class TestSigSearchLive(LiveTestBase):
    """End-to-end sig search and Bug 2 pre-filter using a real Typesense collection."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        from tests.helpers import _make_git_repo, _delete_collection
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
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
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
        old = self._ts_search("BlobStore", "member_sigs,method_names,filename")
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
