"""
Unit tests for sig / listing modes.

Covers:
  - extract_metadata: member_sigs (param types, return types, constructors, locals)
  - q_methods / q_classes / q_fields semantics (declarations only, no call contamination)
  - Narrow pre-filter (Bug 2): listing modes must not include calls-only files
  - Fixed sig search query_by (Bug 1): method_names removed to avoid false positives

Integration tests (require Typesense) are in tests/integration/test_mode_sig.py.

Run (no Typesense):
    pytest tests/unit/test_mode_sig.py -v
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    SIG_HAS_PARAM, CALLS_ONLY, NAME_CONTAINS,
    LISTING_TARGET, CALLS_IBLOBSERVICE,
)
from indexserver.indexer import extract_metadata
from query.cs import q_methods, q_classes, q_fields


# ══════════════════════════════════════════════════════════════════════════════
# extract_metadata — member_sigs field
# ══════════════════════════════════════════════════════════════════════════════

class TestMemberSigs(unittest.TestCase):
    """extract_metadata correctly populates member_sigs."""

    def test_param_type_in_member_sigs(self):
        meta = extract_metadata(SIG_HAS_PARAM.encode(), ".cs")
        assert any("BlobStore" in s for s in meta["member_sigs"]), \
            f"member_sigs: {meta['member_sigs']}"

    def test_return_type_in_member_sigs(self):
        """Return type uses child_by_field_name('returns') — must be captured."""
        meta = extract_metadata(SIG_HAS_PARAM.encode(), ".cs")
        sigs = meta["member_sigs"]
        assert any("BlobStore" in s and "Retrieve" in s for s in sigs), \
            f"Return type 'BlobStore' must appear in Retrieve sig. Got: {sigs}"

    def test_calls_only_no_blobstore_in_member_sigs(self):
        meta = extract_metadata(CALLS_ONLY.encode(), ".cs")
        assert not any("BlobStore" in s for s in meta["member_sigs"]), \
            f"calls-only file must have no BlobStore in member_sigs: {meta['member_sigs']}"

    def test_call_targets_not_in_member_sigs(self):
        meta = extract_metadata(CALLS_ONLY.encode(), ".cs")
        for sig in meta["member_sigs"]:
            assert "FetchBlob" not in sig and "StoreBlob" not in sig, \
                f"call target leaked into member_sigs: {sig!r}"

    def test_constructor_in_member_sigs(self):
        meta = extract_metadata(LISTING_TARGET.encode(), ".cs")
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
        meta = extract_metadata(src.encode(), ".cs")
        assert any("LocalHelper" in s for s in meta["member_sigs"]), \
            f"local function not in member_sigs: {meta['member_sigs']}"

    def test_method_names_field_has_all_names(self):
        meta = extract_metadata(SIG_HAS_PARAM.encode(), ".cs")
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
        meta = extract_metadata(CALLS_IBLOBSERVICE.encode(), ".cs")
        for field in ("member_sigs", "type_refs", "base_types", "class_names"):
            vals = meta[field]
            assert not any("BlobStore" in v for v in vals), \
                f"BlobStore found in narrow-prefilter field '{field}': {vals}"

    def test_sig_file_present_in_narrow_prefilter_fields(self):
        meta = extract_metadata(SIG_HAS_PARAM.encode(), ".cs")
        assert any("BlobStore" in s for s in meta["member_sigs"])

    def test_listing_target_present_in_narrow_prefilter_fields(self):
        meta = extract_metadata(LISTING_TARGET.encode(), ".cs")
        assert any("BlobStore" in s for s in meta["member_sigs"])
        assert "BlobStore" in meta["type_refs"]

    def test_class_name_only_hits_class_names_field(self):
        meta = extract_metadata(NAME_CONTAINS.encode(), ".cs")
        assert "BlobStoreMigrator" in meta["class_names"]
        assert not any("BlobStore" in s for s in meta["member_sigs"])


if __name__ == "__main__":
    unittest.main()
