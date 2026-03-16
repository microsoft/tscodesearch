"""
Tests for param_type mode.

mode: --param-type TYPE (q_param_type)
No dedicated Typesense field: relies on method_sigs and type_refs.

Gaps tested:
  - Methods/constructors with a param typed T are found.
  - Methods with NO param of type T are not returned.
  - Field declarations typed T do NOT appear (that's field_type mode).
  - ref/out/in/params modifiers are handled — the param is still found.
  - Local functions inside method bodies are also checked.
  - Generic param types (IList<T>) match both 'IList' and 'T'.
  - The output includes the enclosing method name and param name.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    PARAM_TYPED_BLOBSTORE_MULTI, FIELD_ONLY_NO_PARAMS, PARAM_TYPED_WITH_MODIFIERS,
    LISTING_TARGET,
)
from indexserver.indexer import extract_cs_metadata
from query import q_uses


# ══════════════════════════════════════════════════════════════════════════════
# q_uses(uses_kind="param") AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQParamType(unittest.TestCase):

    def _ptype(self, src, type_name):
        return q_uses(*_parse(src), type_name, uses_kind="param")

    def test_finds_param_typed_blobstore(self):
        r = self._ptype(PARAM_TYPED_BLOBSTORE_MULTI, "BlobStore")
        assert r, "Methods with BlobStore param must be found"

    def test_finds_all_methods_with_param(self):
        r = self._ptype(PARAM_TYPED_BLOBSTORE_MULTI, "BlobStore")
        texts = [t for _, t in r]
        # Handle, Verify, Dispatch all have BlobStore params
        assert any("Handle"   in t for t in texts), f"Handle not found: {texts}"
        assert any("Verify"   in t for t in texts), f"Verify not found: {texts}"
        assert any("Dispatch" in t for t in texts), f"Dispatch not found: {texts}"

    def test_method_without_param_not_returned(self):
        """Lookup has no BlobStore param — must not appear."""
        r = self._ptype(PARAM_TYPED_BLOBSTORE_MULTI, "BlobStore")
        texts = [t for _, t in r]
        assert not any("Lookup" in t for t in texts), \
            f"Method 'Lookup' (no BlobStore param) must not appear: {texts}"

    def test_field_only_not_returned(self):
        """FIELD_ONLY_NO_PARAMS has BlobStore as a field but NO method has BlobStore param."""
        r = self._ptype(FIELD_ONLY_NO_PARAMS, "BlobStore")
        assert r == [], f"Field-only file must not appear: {r}"

    def test_modifier_params_found(self):
        """ref/out BlobStore params must still be found."""
        r = self._ptype(PARAM_TYPED_WITH_MODIFIERS, "BlobStore")
        assert r, "ref/out BlobStore params must be found"
        texts = [t for _, t in r]
        assert any("TryGet" in t for t in texts)
        assert any("Exchange" in t for t in texts)

    def test_output_includes_param_name(self):
        r = self._ptype(PARAM_TYPED_BLOBSTORE_MULTI, "BlobStore")
        texts = [t for _, t in r]
        # params include "store" or "s"
        assert any("store" in t.lower() for t in texts), \
            f"Param name not in output: {texts}"

    def test_constructor_param_found(self):
        src = """\
namespace Synth {
    public class Service {
        private BlobStore _store;
        public Service(BlobStore store) { _store = store; }
        public void Run(string key) { }
    }
}
"""
        r = self._ptype(src, "BlobStore")
        texts = [t for _, t in r]
        assert any("Service" in t for t in texts), \
            f"Constructor param must be found: {texts}"

    def test_generic_param_matches_inner_type(self):
        src = """\
namespace Synth {
    public class Batch {
        public void Process(IList<BlobStore> stores) { }
        public void Flush(IEnumerable<BlobStore> items) { }
    }
}
"""
        r = self._ptype(src, "BlobStore")
        assert r, "IList<BlobStore> param must match 'BlobStore'"

    def test_local_function_param_found(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            void Inner(BlobStore store) { }
        }
    }
}
"""
        r = self._ptype(src, "BlobStore")
        texts = [t for _, t in r]
        assert any("Inner" in t for t in texts), \
            f"Local function param must be found: {texts}"

    def test_different_type_not_returned(self):
        r = self._ptype(PARAM_TYPED_BLOBSTORE_MULTI, "IRouter")
        texts = [t for _, t in r]
        # Only Dispatch has IRouter param
        assert any("Dispatch" in t for t in texts)
        assert not any("Handle" in t for t in texts), \
            "Handle has no IRouter param"


# ══════════════════════════════════════════════════════════════════════════════
# Metadata consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestParamTypeMetadataConsistency(unittest.TestCase):

    def test_param_type_in_method_sigs(self):
        """When a method has BlobStore param, method_sigs must contain BlobStore."""
        meta = extract_cs_metadata(PARAM_TYPED_BLOBSTORE_MULTI.encode())
        assert any("BlobStore" in s for s in meta["method_sigs"]), \
            f"method_sigs: {meta['method_sigs']}"

    def test_param_type_in_type_refs(self):
        meta = extract_cs_metadata(PARAM_TYPED_BLOBSTORE_MULTI.encode())
        assert "BlobStore" in meta["type_refs"]

    def test_field_only_file_no_blobstore_in_sigs(self):
        meta = extract_cs_metadata(FIELD_ONLY_NO_PARAMS.encode())
        # field type IS in type_refs, but NOT in method_sigs (no param)
        assert "BlobStore" in meta["type_refs"]
        assert not any("BlobStore" in s for s in meta["method_sigs"]), \
            "Field-only file must have no BlobStore in method_sigs"

    def test_field_only_consistency_with_q_param_type(self):
        """q_param_type returns empty for field-only file, but type_refs has BlobStore.
        This confirms the two modes are correctly distinct."""
        meta = extract_cs_metadata(FIELD_ONLY_NO_PARAMS.encode())
        r    = q_uses(*_parse(FIELD_ONLY_NO_PARAMS), "BlobStore", uses_kind="param")
        assert "BlobStore" in meta["type_refs"]  # uses mode would find this
        assert r == []                            # but param_type mode would not


if __name__ == "__main__":
    unittest.main()
