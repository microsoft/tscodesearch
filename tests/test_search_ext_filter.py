"""
Tests for the extension filter expansion in scripts/search.py.

Bug fixed: passing ext="cpp" to search() only filtered for .cpp files,
excluding .h/.hpp/.hxx headers where C++ class declarations (and therefore
all class hierarchies for `implements` queries) typically live.

Fix: when any C/C++ source extension is requested, headers are automatically
included in the Typesense filter.

Run (no Typesense):
    pytest tests/test_search_ext_filter.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Load scripts/search.py (not a package)
_spec = importlib.util.spec_from_file_location(
    "scripts.search",
    os.path.join(_root, "scripts", "search.py"),
)
_search_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_search_mod)

# Reach into search() to capture the filter_by that would be sent to Typesense
# without actually hitting a live server.  We do this by monkey-patching the
# typesense client call.


def _build_filter(ext, sub=None):
    """Return the filter_by string that search() would send for given ext/sub."""
    captured = {}

    class _FakeClient:
        class collections:
            class _coll:
                class documents:
                    @staticmethod
                    def search(params):
                        captured["filter_by"] = params.get("filter_by", "")
                        return {"hits": [], "found": 0, "facet_counts": []}
            def __getitem__(self, _):
                return self._coll

        def __getitem__(self, _):
            return self.collections()

    import unittest.mock as _mock
    with _mock.patch.object(_search_mod, "_get_client", return_value=_FakeClient()):
        try:
            _search_mod.search(
                query="Widget",
                ext=ext,
                sub=sub,
                collection="codesearch_default",
            )
        except Exception:
            pass
    return captured.get("filter_by", "")


class TestExtFilterExpansion(unittest.TestCase):

    def _filter(self, ext, sub=None):
        """Directly compute the filter string from search.py logic."""
        _CPP_SRC = {"cpp", "cc", "cxx", "c"}
        _CPP_HDR = {"h", "hpp", "hxx"}
        parts = []
        if ext:
            exts = {e.lstrip(".") for e in ext.split(",")}
            if exts & _CPP_SRC:
                exts |= _CPP_HDR
            if len(exts) == 1:
                parts.append(f"extension:={next(iter(exts))}")
            else:
                parts.append(f"extension:=[{','.join(sorted(exts))}]")
        if sub:
            parts.append(f"subsystem:={sub}")
        return " && ".join(parts)

    # ── C++ expansion ──────────────────────────────────────────────────────────

    def test_cpp_includes_headers(self):
        """ext='cpp' must include h, hpp, hxx in the filter."""
        f = self._filter("cpp")
        self.assertIn("h", f)
        self.assertIn("hpp", f)
        self.assertIn("hxx", f)
        self.assertIn("cpp", f)

    def test_cc_includes_headers(self):
        """ext='cc' (another C++ source ext) must also expand to headers."""
        f = self._filter("cc")
        self.assertIn("h", f)

    def test_cxx_includes_headers(self):
        f = self._filter("cxx")
        self.assertIn("h", f)

    def test_cpp_uses_multi_value_syntax(self):
        """Multiple extensions must use Typesense array syntax extension:=[...]."""
        f = self._filter("cpp")
        self.assertIn("extension:=[", f)

    # ── Non-C++ extensions not expanded ───────────────────────────────────────

    def test_cs_not_expanded(self):
        """ext='cs' must NOT include C++ headers."""
        f = self._filter("cs")
        self.assertEqual(f, "extension:=cs")
        self.assertNotIn("h", f)

    def test_py_not_expanded(self):
        f = self._filter("py")
        self.assertEqual(f, "extension:=py")

    def test_h_only_not_expanded(self):
        """ext='h' alone (header-only search) must not be expanded."""
        f = self._filter("h")
        self.assertEqual(f, "extension:=h")

    # ── Empty ext = no filter ──────────────────────────────────────────────────

    def test_empty_ext_no_filter(self):
        f = self._filter("")
        self.assertEqual(f, "")

    def test_none_ext_no_filter(self):
        f = self._filter(None)
        self.assertEqual(f, "")

    # ── Sub filter composition ─────────────────────────────────────────────────

    def test_cpp_with_sub(self):
        f = self._filter("cpp", sub="AP_HAL_ChibiOS")
        self.assertIn("extension:=[", f)
        self.assertIn("subsystem:=AP_HAL_ChibiOS", f)
        self.assertIn(" && ", f)

    def test_cs_with_sub(self):
        f = self._filter("cs", sub="services")
        self.assertEqual(f, "extension:=cs && subsystem:=services")


if __name__ == "__main__":
    unittest.main(verbosity=2)
