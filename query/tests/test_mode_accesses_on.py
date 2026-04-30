"""
Tests for member_accesses mode.

mode: --member-accesses TYPE (q_member_accesses)

Finds every .Member access on locals and parameters declared as (or inferred as)
TYPE.  Useful for discovering what callers read from a value of a given type.

Gaps tested:
  - Explicitly typed parameters are tracked.
  - Explicitly typed fields are tracked.
  - Accesses on a different type (e.g. string) are NOT returned.
  - var-inferred locals from new TYPE() are tracked.
  - var-inferred from 'as TYPE' cast are tracked.
  - Only accesses on the correct variable are included.
  - Files with no TYPE declarations return empty.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    MEMBER_ACCESS_BLOBSTORE_PARAM, MEMBER_ACCESS_BLOBSTORE_FIELD,
    MEMBER_ACCESS_INTERFACE_ONLY, MEMBER_ACCESS_VAR_INFERRED,
    USES_BLOBSTORE_NO_CAST,
)
from ..cs import q_accesses_on


# ══════════════════════════════════════════════════════════════════════════════
# q_member_accesses AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQMemberAccesses(unittest.TestCase):

    def _accesses(self, src, type_name):
        return q_accesses_on(*_parse(src), type_name=type_name)

    def test_finds_param_accesses(self):
        r = self._accesses(MEMBER_ACCESS_BLOBSTORE_PARAM, "BlobStore")
        assert r, "Member accesses on BlobStore param must be found"

    def test_finds_all_members_on_param(self):
        r = self._accesses(MEMBER_ACCESS_BLOBSTORE_PARAM, "BlobStore")
        {t.split(".")[1].split(" ")[0] if "." in t else t
                   for _, t in r}
        # Write, Size, Flush are accessed on 'store'
        text = " ".join(t for _, t in r)
        assert "Write" in text, f"Write access missing: {r}"
        assert "Size"  in text, f"Size access missing: {r}"
        assert "Flush" in text, f"Flush access missing: {r}"

    def test_does_not_return_string_accesses(self):
        """Accesses on 's' (string type) must not appear."""
        r = self._accesses(MEMBER_ACCESS_BLOBSTORE_PARAM, "BlobStore")
        text = " ".join(t for _, t in r)
        assert "Length" not in text, \
            f"String.Length must not appear for BlobStore search: {r}"

    def test_finds_field_accesses(self):
        r = self._accesses(MEMBER_ACCESS_BLOBSTORE_FIELD, "BlobStore")
        assert r, "Accesses on BlobStore field must be found"
        text = " ".join(t for _, t in r)
        assert "Read" in text, f"Read access missing: {r}"

    def test_interface_type_not_returned_for_concrete_type(self):
        """MEMBER_ACCESS_INTERFACE_ONLY only holds IBlobStore, not BlobStore."""
        r = self._accesses(MEMBER_ACCESS_INTERFACE_ONLY, "BlobStore")
        assert r == [], \
            f"IBlobStore field must not match BlobStore search: {r}"

    def test_var_inferred_object_creation(self):
        """var x = new BlobStore() — accesses on x must be found."""
        r = self._accesses(MEMBER_ACCESS_VAR_INFERRED, "BlobStore")
        assert r, "Accesses on var-inferred BlobStore must be found"
        text = " ".join(t for _, t in r)
        assert "Write" in text
        assert "Flush" in text

    def test_no_declarations_returns_empty(self):
        """MEMBER_ACCESS_INTERFACE_ONLY has no BlobStore declarations."""
        r = self._accesses(MEMBER_ACCESS_INTERFACE_ONLY, "BlobStore")
        assert r == []

    def test_as_cast_variable_tracked(self):
        src = """\
namespace Synth {
    public class C {
        public void Run(object o) {
            var store = o as BlobStore;
            store.Write(\"k\", null);
            store.Flush();
        }
    }
}
"""
        r = self._accesses(src, "BlobStore")
        assert r, "as-cast variable must be tracked"
        text = " ".join(t for _, t in r)
        assert "Write" in text

    def test_explicit_cast_variable_tracked(self):
        src = """\
namespace Synth {
    public class C {
        public void Run(object o) {
            var store = (BlobStore)o;
            store.Write(\"k\", null);
        }
    }
}
"""
        r = self._accesses(src, "BlobStore")
        assert r, "Explicit-cast variable must be tracked"
        text = " ".join(t for _, t in r)
        assert "Write" in text

    def test_correct_type_only(self):
        src = """\
namespace Synth {
    public class C {
        public void Run(BlobStore store, ILogger log) {
            store.Write(\"k\", null);
            log.Info(\"done\");
        }
    }
}
"""
        r_bs  = self._accesses(src, "BlobStore")
        r_log = self._accesses(src, "ILogger")
        bs_text  = " ".join(t for _, t in r_bs)
        log_text = " ".join(t for _, t in r_log)
        assert "Write" in bs_text,  f"Write must appear for BlobStore: {r_bs}"
        assert "Info"  in log_text, f"Info must appear for ILogger: {r_log}"
        assert "Info"  not in bs_text,  "ILogger.Info must not appear for BlobStore"
        assert "Write" not in log_text, "BlobStore.Write must not appear for ILogger"

    def test_unrelated_file_empty(self):
        r = self._accesses(USES_BLOBSTORE_NO_CAST, "WidgetService")
        assert r == []


if __name__ == "__main__":
    unittest.main()
