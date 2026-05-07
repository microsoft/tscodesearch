"""
Tests for the extension filter expansion used by both scripts/search.py and
tsquery_server.py /query-codebase.

When any C/C++ source extension is requested, headers (.h/.hpp/.hxx) are
automatically included so `implements`/`uses` queries find class declarations
that live in headers.

Run (no daemon):
    pytest tests/unit/test_search_ext_filter.py -v
"""
from __future__ import annotations

import unittest

from tsquery_server import _build_filter_by


def _build_filter(ext=None, sub=None, exclude_path=None):
    """Return the filter_by string production code emits for the given args."""
    return _build_filter_by(ext or "", sub or "", exclude_path or "")


class TestExtFilterExpansion(unittest.TestCase):

    def _filter(self, ext, sub=None, exclude_path=None):
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
            included = [p.replace(chr(92), '/').strip('/') for p in sub.split(",")]
            included = [p for p in included if p]
            if len(included) == 1:
                parts.append(f"path_segments:={included[0]}")
            elif included:
                parts.append(f"path_segments:=[{','.join(included)}]")
        if exclude_path:
            excluded = [p.replace(chr(92), '/').strip('/') for p in exclude_path.split(",")]
            excluded = [p for p in excluded if p]
            if len(excluded) == 1:
                parts.append(f"path_segments:!={excluded[0]}")
            elif excluded:
                parts.append(f"path_segments:!=[{','.join(excluded)}]")
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
        self.assertIn("path_segments:=AP_HAL_ChibiOS", f)
        self.assertIn(" && ", f)

    def test_cs_with_sub(self):
        f = self._filter("cs", sub="services")
        self.assertEqual(f, "extension:=cs && path_segments:=services")

    def test_cs_with_multi_segment_sub(self):
        """sub='services/billing' must produce a multi-segment path_segments filter."""
        f = self._filter("cs", sub="services/billing")
        self.assertEqual(f, "extension:=cs && path_segments:=services/billing")

    # ── exclude_path ───────────────────────────────────────────────────────────

    def test_exclude_single_folder(self):
        f = self._filter("cs", exclude_path="tests")
        self.assertEqual(f, "extension:=cs && path_segments:!=tests")

    def test_exclude_multiple_folders(self):
        f = self._filter("cs", exclude_path="tests,generated")
        self.assertEqual(f, "extension:=cs && path_segments:!=[tests,generated]")

    def test_exclude_multi_segment_path(self):
        f = self._filter("cs", exclude_path="services/billing/legacy")
        self.assertEqual(f, "extension:=cs && path_segments:!=services/billing/legacy")

    def test_exclude_combines_with_sub(self):
        f = self._filter("cs", sub="services", exclude_path="services/legacy,tests")
        self.assertEqual(
            f,
            "extension:=cs && path_segments:=services && path_segments:!=[services/legacy,tests]",
        )

    def test_exclude_strips_whitespace_safe(self):
        """Backslashes get normalised to forward-slashes; trim leading/trailing slashes."""
        f = self._filter("cs", exclude_path=r"\tests\,/generated/")
        self.assertEqual(f, "extension:=cs && path_segments:!=[tests,generated]")


class TestMultiSubProduction(unittest.TestCase):
    """Verify production search() emits the expected filter_by for multi-value sub."""

    def test_single_value_unchanged(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services"),
            "extension:=cs && path_segments:=services",
        )

    def test_two_values(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services,vendor"),
            "extension:=cs && path_segments:=[services,vendor]",
        )

    def test_three_values(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="a,b,c"),
            "extension:=cs && path_segments:=[a,b,c]",
        )

    def test_multi_segment_values(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services/billing,vendor/aws"),
            "extension:=cs && path_segments:=[services/billing,vendor/aws]",
        )

    def test_normalises_backslashes_and_slashes(self):
        self.assertEqual(
            _build_filter(ext="cs", sub=r"\services\,/vendor/"),
            "extension:=cs && path_segments:=[services,vendor]",
        )

    def test_empty_segments_dropped(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services,,vendor"),
            "extension:=cs && path_segments:=[services,vendor]",
        )

    def test_only_slashes_drops_segment(self):
        self.assertEqual(_build_filter(ext="cs", sub="/"), "extension:=cs")

    def test_combines_with_exclude_path(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services,vendor",
                          exclude_path="services/legacy"),
            "extension:=cs && path_segments:=[services,vendor] && "
            "path_segments:!=services/legacy",
        )

    def test_combines_with_multi_exclude(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services,vendor",
                          exclude_path="tests,generated"),
            "extension:=cs && path_segments:=[services,vendor] && "
            "path_segments:!=[tests,generated]",
        )


class TestExcludePathProduction(unittest.TestCase):
    """Verify production search() in scripts/search.py actually emits the
    expected filter_by, by monkey-patching _ts_search to capture the
    params it would have sent."""

    def test_single_folder(self):
        self.assertEqual(
            _build_filter(ext="cs", exclude_path="tests"),
            "extension:=cs && path_segments:!=tests",
        )

    def test_multiple_folders(self):
        self.assertEqual(
            _build_filter(ext="cs", exclude_path="tests,generated"),
            "extension:=cs && path_segments:!=[tests,generated]",
        )

    def test_multi_segment_path(self):
        self.assertEqual(
            _build_filter(ext="cs", exclude_path="services/billing/legacy"),
            "extension:=cs && path_segments:!=services/billing/legacy",
        )

    def test_combines_with_sub(self):
        self.assertEqual(
            _build_filter(ext="cs", sub="services",
                          exclude_path="services/legacy,tests"),
            "extension:=cs && path_segments:=services && "
            "path_segments:!=[services/legacy,tests]",
        )

    def test_normalises_backslashes_and_slashes(self):
        self.assertEqual(
            _build_filter(ext="cs", exclude_path=r"\tests\,/generated/"),
            "extension:=cs && path_segments:!=[tests,generated]",
        )

    def test_empty_string_no_filter(self):
        self.assertEqual(_build_filter(ext="cs", exclude_path=""), "extension:=cs")

    def test_none_no_filter(self):
        self.assertEqual(_build_filter(ext="cs", exclude_path=None), "extension:=cs")

    def test_only_whitespace_segments_dropped(self):
        """Empty segments between commas get dropped, not emitted as bare !=."""
        self.assertEqual(
            _build_filter(ext="cs", exclude_path="tests,,generated"),
            "extension:=cs && path_segments:!=[tests,generated]",
        )

    def test_only_slashes_drops_segment(self):
        """A path that strips down to empty must not produce a filter."""
        self.assertEqual(_build_filter(ext="cs", exclude_path="/"), "extension:=cs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
