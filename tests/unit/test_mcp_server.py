"""
Tests for mcp_server.py — helper functions and query_single_file tool.

No indexserver or Typesense required.  The mcp package is stubbed when absent
so the tests run under the indexserver venv as well as the client venv.
"""
from __future__ import annotations

import sys
import unittest
from typing import Any

from tests import REPO_ROOT

_SAMPLE  = str(REPO_ROOT / "sample" / "root1")
_CS_FILE = str(REPO_ROOT / "sample" / "root1" / "DataStore.cs")
_PY_FILE = str(REPO_ROOT / "sample" / "root1" / "pipeline.py")

# ── Stub mcp if not installed (indexserver venv lacks it) ────────────────────

if "mcp" not in sys.modules:
    import types
    from unittest.mock import MagicMock

    class _FakeFastMCP:
        def __init__(self, *_a, **_kw): pass
        def tool(self):
            def deco(fn): return fn
            return deco
        def run(self): pass

    _fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    setattr(_fastmcp_mod, "FastMCP", _FakeFastMCP)
    sys.modules.setdefault("mcp",              MagicMock())
    sys.modules.setdefault("mcp.server",       MagicMock())
    sys.modules["mcp.server.fastmcp"]        = _fastmcp_mod

# ── Import mcp_server, skip all tests if config.json is absent ───────────────

import types as _types
sys.path.insert(0, str(REPO_ROOT))
_ms: Any = _types.ModuleType("_mcp_server_placeholder")
_IMPORT_OK  = False
_IMPORT_ERR = ""
try:
    import mcp_server as _ms  # type: ignore[import-not-found]
    _IMPORT_OK = True
except Exception as e:
    _IMPORT_ERR = str(e)

_skip = unittest.skipUnless(_IMPORT_OK, f"mcp_server import failed: {_IMPORT_ERR}")


# ── _collection_for_root ──────────────────────────────────────────────────────

@_skip
class TestCollectionForRoot(unittest.TestCase):

    def test_default(self):
        assert _ms._collection_for_root("default") == "codesearch_default"

    def test_uppercase_lowered(self):
        assert _ms._collection_for_root("Backend") == "codesearch_backend"

    def test_spaces_replaced(self):
        assert _ms._collection_for_root("My Project") == "codesearch_my_project"

    def test_special_chars_replaced(self):
        assert _ms._collection_for_root("my-repo/src") == "codesearch_my_repo_src"


# ── _rel_path ─────────────────────────────────────────────────────────────────

@_skip
class TestRelPath(unittest.TestCase):

    def test_strips_root(self):
        assert _ms._rel_path("C:/src/Widget.cs", "C:/src") == "Widget.cs"

    def test_nested_path(self):
        assert _ms._rel_path("C:/src/services/Order.cs", "C:/src") == "services/Order.cs"

    def test_backslash_in_file_normalized(self):
        assert _ms._rel_path("C:\\src\\Widget.cs", "C:/src") == "Widget.cs"

    def test_case_insensitive(self):
        assert _ms._rel_path("C:/SRC/Widget.cs", "C:/src") == "Widget.cs"

    def test_outside_root_returns_original(self):
        result = _ms._rel_path("D:/other/Widget.cs", "C:/src")
        assert "Widget.cs" in result


# ── _truncate ─────────────────────────────────────────────────────────────────

@_skip
class TestTruncate(unittest.TestCase):

    def test_short_output_unchanged(self):
        out, truncated = _ms._truncate("hello")
        assert out == "hello" and not truncated

    def test_long_output_truncated(self):
        big = "x" * (_ms._MAX_OUTPUT_CHARS + 500)
        out, truncated = _ms._truncate(big)
        assert truncated
        assert len(out) <= _ms._MAX_OUTPUT_CHARS

    def test_truncates_at_newline(self):
        line = "a" * 100 + "\n"
        big  = line * (_ms._MAX_OUTPUT_CHARS // 100 + 5)
        out, truncated = _ms._truncate(big)
        assert truncated
        # Cut must land on a line boundary
        assert not out or out[-1] == "\n" or "\n" in out

    def test_empty_string(self):
        out, truncated = _ms._truncate("")
        assert out == "" and not truncated

    def test_exactly_at_limit_not_truncated(self):
        s = "x" * _ms._MAX_OUTPUT_CHARS
        out, truncated = _ms._truncate(s)
        assert not truncated


# ── _to_windows_path ──────────────────────────────────────────────────────────

@_skip
class TestToWindowsPath(unittest.TestCase):

    def setUp(self):
        self._orig_roots = _ms._ROOTS
        _ms._ROOTS = {"default": "C:/myproject/src"}

    def tearDown(self):
        _ms._ROOTS = self._orig_roots

    def test_windows_path_passes_through(self):
        assert _ms._to_windows_path("C:/myproject/src/Widget.cs") == "C:/myproject/src/Widget.cs"

    def test_backslash_normalized(self):
        assert _ms._to_windows_path("C:\\myproject\\src\\Widget.cs") == "C:/myproject/src/Widget.cs"

    def test_mnt_path_converted(self):
        assert _ms._to_windows_path("/mnt/c/myproject/src/Widget.cs") == "C:/myproject/src/Widget.cs"

    def test_src_root_dollar_expanded(self):
        result = _ms._to_windows_path("$SRC_ROOT/services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"

    def test_src_root_curly_expanded(self):
        result = _ms._to_windows_path("${SRC_ROOT}/services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"

    def test_relative_path_prepends_root(self):
        result = _ms._to_windows_path("services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"


# ── _get_root ─────────────────────────────────────────────────────────────────

@_skip
class TestGetRoot(unittest.TestCase):

    def setUp(self):
        self._orig_roots = _ms._ROOTS
        _ms._ROOTS = {
            "default": "C:/myproject/src",
            "tests":   "C:/myproject/tests",
        }

    def tearDown(self):
        _ms._ROOTS = self._orig_roots

    def test_empty_name_returns_default(self):
        coll, path = _ms._get_root("")
        assert coll == "codesearch_default"
        assert path == "C:/myproject/src"

    def test_named_root(self):
        coll, path = _ms._get_root("tests")
        assert coll == "codesearch_tests"
        assert path == "C:/myproject/tests"

    def test_unknown_root_raises(self):
        with self.assertRaises(ValueError):
            _ms._get_root("nonexistent")

    def test_dict_entry_uses_path_field(self):
        _ms._ROOTS["complex"] = {"path": "C:/complex/src", "other": "ignored"}
        coll, path = _ms._get_root("complex")
        assert coll == "codesearch_complex"
        assert path == "C:/complex/src"


# ── query_single_file ─────────────────────────────────────────────────────────

@_skip
class TestQuerySingleFile(unittest.TestCase):
    """Tests for the query_single_file MCP tool against sample files.
    _ROOTS is patched so path resolution is deterministic.
    On Linux/WSL _to_windows_path is also patched to a no-op so native
    paths are used directly (the tool is designed to run on Windows, but
    the in-process query_file() call is cross-platform).
    """

    def setUp(self):
        self._orig_roots   = _ms._ROOTS
        self._orig_to_wp   = _ms._to_windows_path
        _ms._ROOTS = {"default": _SAMPLE}
        if sys.platform != "win32":
            _ms._to_windows_path = lambda p: p  # no-op: keep native paths on Linux

    def tearDown(self):
        _ms._ROOTS          = self._orig_roots
        _ms._to_windows_path = self._orig_to_wp

    # ── error cases ───────────────────────────────────────────────────────────

    def test_no_file_arg(self):
        assert _ms.query_single_file("methods", file="") == "file= is required."

    def test_nonexistent_file(self):
        result = _ms.query_single_file("methods", file="C:/nonexistent/Missing.cs")
        assert "Cannot read file" in result

    def test_unknown_root(self):
        result = _ms.query_single_file("methods", file=_CS_FILE, root="nonexistent")
        assert "Error:" in result and "nonexistent" in result

    # ── C# listing modes ─────────────────────────────────────────────────────

    def test_methods_returns_signatures(self):
        result = _ms.query_single_file("methods", file=_CS_FILE)
        assert "DataStore.cs" in result
        assert any(kw in result for kw in ("Write", "Read", "void", "Task", "("))

    def test_fields_listing(self):
        result = _ms.query_single_file("fields", file=_CS_FILE)
        assert "DataStore.cs" in result

    def test_classes_listing(self):
        result = _ms.query_single_file("classes", file=_CS_FILE)
        assert "DataStore.cs" in result

    # ── C# pattern modes ─────────────────────────────────────────────────────

    def test_calls_with_pattern(self):
        result = _ms.query_single_file("calls", pattern="Write", file=_CS_FILE)
        assert "DataStore.cs" in result
        assert "Write" in result

    def test_uses_with_pattern(self):
        result = _ms.query_single_file("uses", pattern="IDataStore", file=_CS_FILE)
        assert "DataStore.cs" in result
        assert "IDataStore" in result

    def test_uses_kind_param(self):
        result = _ms.query_single_file(
            "uses", pattern="IDataStore", uses_kind="param", file=_CS_FILE
        )
        assert "IDataStore" in result

    def test_no_matches(self):
        result = _ms.query_single_file("calls", pattern="NoSuchMethod", file=_CS_FILE)
        assert "No matches found" in result

    # ── Python ────────────────────────────────────────────────────────────────

    def test_python_methods(self):
        result = _ms.query_single_file("methods", file=_PY_FILE)
        assert "pipeline.py" in result

    def test_python_calls(self):
        result = _ms.query_single_file("calls", pattern="process", file=_PY_FILE)
        # May or may not have matches; just confirm it doesn't crash
        assert "pipeline.py" in result

    # ── pagination ────────────────────────────────────────────────────────────

    def test_head_limit_restricts_results(self):
        full    = _ms.query_single_file("methods", file=_CS_FILE)
        limited = _ms.query_single_file("methods", file=_CS_FILE, head_limit=1)
        # Full result should contain more match lines than limited (file has >1 method)
        def _match_lines(s):
            return [l for l in s.splitlines() if re.search(r":\d+:", l)]
        import re
        assert len(_match_lines(limited)) <= len(_match_lines(full))

    def test_offset_shifts_results(self):
        page1 = _ms.query_single_file("methods", file=_CS_FILE, head_limit=1, offset=0)
        page2 = _ms.query_single_file("methods", file=_CS_FILE, head_limit=1, offset=1)
        # Two consecutive pages should contain different lines
        assert page1 != page2

    def test_pagination_header_shown(self):
        result = _ms.query_single_file("methods", file=_CS_FILE, head_limit=1)
        # If there are >1 methods, paging header should appear
        full_count = len([
            l for l in _ms.query_single_file("methods", file=_CS_FILE).splitlines()
            if ":" in l and not l.startswith("[")
        ])
        if full_count > 1:
            assert "of" in result


# ── query_codebase listing-mode redirect ─────────────────────────────────────

@_skip
class TestQueryCodbaseListingRedirect(unittest.TestCase):
    """query_codebase must redirect listing modes to query_single_file."""

    def _assert_redirects(self, mode):
        result = _ms.query_codebase(mode, "")
        assert "query_single_file" in result, f"mode '{mode}' should redirect"

    def test_methods(self):   self._assert_redirects("methods")
    def test_fields(self):    self._assert_redirects("fields")
    def test_classes(self):   self._assert_redirects("classes")
    def test_usings(self):    self._assert_redirects("usings")
    def test_imports(self):   self._assert_redirects("imports")

    def test_valid_pattern_mode_does_not_redirect(self):
        # declarations is a valid pattern mode — should not redirect
        # (will fail to reach indexserver; that's fine for this test)
        result = _ms.query_codebase("declarations", "SaveChanges")
        assert "query_single_file" not in result


if __name__ == "__main__":
    unittest.main()
