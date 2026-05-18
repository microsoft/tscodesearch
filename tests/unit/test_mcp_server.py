"""
Tests for mcp_server.py — helper functions and query_single_file tool.

No daemon required. The mcp package is stubbed when absent so the tests run
under any Python with tree-sitter installed.
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


# ── collection_for_root (indexserver.config) ─────────────────────────────────

@_skip
class TestCollectionForRoot(unittest.TestCase):

    def test_default(self):
        from indexserver.config import collection_for_root
        assert collection_for_root("default") == "codesearch_default"

    def test_uppercase_lowered(self):
        from indexserver.config import collection_for_root
        assert collection_for_root("Backend") == "codesearch_backend"

    def test_spaces_replaced(self):
        from indexserver.config import collection_for_root
        assert collection_for_root("My Project") == "codesearch_my_project"

    def test_special_chars_replaced(self):
        from indexserver.config import collection_for_root
        assert collection_for_root("my-repo/src") == "codesearch_my_repo_src"


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

def _root(name, path):
    """Build a Root instance matching what load_config produces."""
    from indexserver.config import Root, collection_for_root, INCLUDE_EXTENSIONS
    return Root(name=name, path=path, collection=collection_for_root(name),
                extensions=INCLUDE_EXTENSIONS)


@_skip
class TestToWindowsPath(unittest.TestCase):

    def setUp(self):
        self._orig_roots = _ms._ROOTS
        _ms._ROOTS = {"default": _root("default", "C:/myproject/src")}

    def tearDown(self):
        _ms._ROOTS = self._orig_roots

    def test_windows_path_passes_through(self):
        assert _ms._to_windows_path("C:/myproject/src/Widget.cs") == "C:/myproject/src/Widget.cs"

    def test_backslash_normalized(self):
        assert _ms._to_windows_path("C:\\myproject\\src\\Widget.cs") == "C:/myproject/src/Widget.cs"

    def test_src_root_dollar_expanded(self):
        result = _ms._to_windows_path("$SRC_ROOT/services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"

    def test_src_root_curly_expanded(self):
        result = _ms._to_windows_path("${SRC_ROOT}/services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"

    def test_relative_path_prepends_root(self):
        result = _ms._to_windows_path("services/Widget.cs")
        assert result == "C:/myproject/src/services/Widget.cs"


# ── _resolve_root ─────────────────────────────────────────────────────────────

@_skip
class TestResolveRoot(unittest.TestCase):

    def setUp(self):
        self._orig_roots = _ms._ROOTS
        _ms._ROOTS = {
            "default": _root("default", "C:/myproject/src"),
            "tests":   _root("tests",   "C:/myproject/tests"),
        }
        # _cfg.roots is what Config.get_root reads, so keep them in sync.
        self._orig_cfg_roots = _ms._cfg.roots
        object.__setattr__(_ms._cfg, "roots", _ms._ROOTS)

    def tearDown(self):
        _ms._ROOTS = self._orig_roots
        object.__setattr__(_ms._cfg, "roots", self._orig_cfg_roots)

    def test_empty_name_returns_default(self):
        r = _ms._resolve_root("")
        assert r.collection == "codesearch_default"
        assert r.path == "C:/myproject/src"

    def test_named_root(self):
        r = _ms._resolve_root("tests")
        assert r.collection == "codesearch_tests"
        assert r.path == "C:/myproject/tests"

    def test_unknown_root_raises(self):
        with self.assertRaises(ValueError):
            _ms._resolve_root("nonexistent")

    def test_no_roots_raises(self):
        _ms._ROOTS = {}
        object.__setattr__(_ms._cfg, "roots", _ms._ROOTS)
        with self.assertRaises(ValueError):
            _ms._resolve_root("")


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
        self._orig_roots     = _ms._ROOTS
        self._orig_cfg_roots = _ms._cfg.roots
        self._orig_to_wp     = _ms._to_windows_path
        _ms._ROOTS = {"default": _root("default", _SAMPLE)}
        object.__setattr__(_ms._cfg, "roots", _ms._ROOTS)
        if sys.platform != "win32":
            _ms._to_windows_path = lambda p: p  # no-op: keep native paths on Linux

    def tearDown(self):
        _ms._ROOTS = self._orig_roots
        object.__setattr__(_ms._cfg, "roots", self._orig_cfg_roots)
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

    # ── all_refs mode ─────────────────────────────────────────────────────────

    def test_all_refs_cs_finds_identifier(self):
        result = _ms.query_single_file("all_refs", pattern="IDataStore", file=_CS_FILE)
        assert "DataStore.cs" in result
        assert "IDataStore" in result

    def test_all_refs_python(self):
        result = _ms.query_single_file("all_refs", pattern="process", file=_PY_FILE)
        assert "pipeline.py" in result

    def test_all_refs_no_match(self):
        result = _ms.query_single_file("all_refs", pattern="NoSuchSymbolXYZ", file=_CS_FILE)
        assert "No matches found" in result

    def test_unknown_mode_reports_supported_modes(self):
        # Unknown modes should error out with the list of supported modes
        # rather than silently returning nothing.
        result = _ms.query_single_file("nonsense_mode", pattern="IDataStore", file=_CS_FILE)
        assert "Error" in result
        assert "all_refs" in result or "Supported modes" in result

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
    def test_usings(self):    self._assert_redirects("imports")
    def test_imports(self):   self._assert_redirects("imports")

    def test_valid_pattern_mode_does_not_redirect(self):
        # declarations is a valid pattern mode — should not redirect
        # (will fail to reach indexserver; that's fine for this test)
        result = _ms.query_codebase("declarations", "SaveChanges")
        assert "query_single_file" not in result


# ── query_codebase overflow drill-down (multi-sub) ───────────────────────────

@_skip
class TestQueryCodebaseOverflowDrilldown(unittest.TestCase):
    """The overflow folder breakdown must compute next-depth subfolders
    correctly, including when sub= is a comma-separated multi-value scope."""

    def setUp(self):
        self._orig_post = _ms._post
        self._orig_warn = _ms._queue_warning
        _ms._queue_warning = lambda: ""

    def tearDown(self):
        _ms._post          = self._orig_post
        _ms._queue_warning = self._orig_warn

    def _install(self, facet_values):
        """Make _post return an overflow response with the given path_segments facet."""
        counts = [{"value": v, "count": n} for v, n in facet_values]
        def _fake_post(path, body, timeout=120):
            return 200, {
                "overflow":     True,
                "found":        500,
                "hits":         [],
                "facet_counts": [{"field_name": "path_segments", "counts": counts}],
            }
        _ms._post = _fake_post

    def test_no_scope_lists_top_level(self):
        self._install([("services", 100), ("vendor", 80),
                       ("services/billing", 50), ("vendor/aws", 30)])
        result = _ms.query_codebase("calls", "Foo")
        # Top-level folders only — no slashes in suggested paths.
        assert 'sub="services"' in result
        assert 'sub="vendor"' in result
        assert 'sub="services/billing"' not in result
        assert 'sub="vendor/aws"' not in result

    def test_single_scope_drills_into_next_depth(self):
        self._install([("services", 200),
                       ("services/billing", 90), ("services/orders", 60),
                       ("services/billing/legacy", 40)])
        result = _ms.query_codebase("calls", "Foo", sub="services")
        # Next depth (depth 2) — billing and orders, but not the depth-3 legacy.
        assert 'sub="services/billing"' in result
        assert 'sub="services/orders"' in result
        assert 'sub="services/billing/legacy"' not in result

    def test_multi_scope_drills_into_each(self):
        """sub='services,vendor' must list next-depth subfolders under both."""
        self._install([
            ("services", 200), ("vendor", 150),
            ("services/billing", 90), ("services/orders", 60),
            ("vendor/aws", 70), ("vendor/gcp", 40),
            ("other/unrelated", 20),  # outside both scopes — must not appear
        ])
        result = _ms.query_codebase("calls", "Foo", sub="services,vendor")
        assert 'sub="services/billing"' in result
        assert 'sub="services/orders"' in result
        assert 'sub="vendor/aws"' in result
        assert 'sub="vendor/gcp"' in result
        assert 'sub="other/unrelated"' not in result
        # Header should reflect the combined scope.
        assert "services,vendor" in result

    def test_multi_scope_dedupes_overlapping_facets(self):
        """If both scopes happen to surface the same value, it appears once."""
        self._install([
            ("a", 50), ("b", 50),
            ("a/shared", 10), ("b/shared", 10),
        ])
        result = _ms.query_codebase("calls", "Foo", sub="a,b")
        # Each unique value emits exactly one suggestion line.
        assert result.count('sub="a/shared"') == 1
        assert result.count('sub="b/shared"') == 1

    def test_tier1_does_not_suggest_query_single_file(self):
        """Tier 1 is the drill-down path — no query_single_file suggestion."""
        self._install([("services", 100), ("vendor", 80)])
        result = _ms.query_codebase("calls", "Foo")
        assert "query_single_file" not in result


# ── query_codebase tier 2 (filenames + hit counts) ───────────────────────────

@_skip
class TestQueryCodebaseTier2(unittest.TestCase):
    """Tier 2: 20+ files with AST matches → filenames + counts, sorted desc."""

    def setUp(self):
        self._orig_post = _ms._post
        self._orig_warn = _ms._queue_warning
        _ms._queue_warning = lambda: ""

    def tearDown(self):
        _ms._post          = self._orig_post
        _ms._queue_warning = self._orig_warn

    def _install_files_with_matches(self, file_match_counts):
        """file_match_counts: list[(rel, n_matches)] — synthesise AST hits."""
        hits = []
        for rel, n in file_match_counts:
            hits.append({
                "document": {"id": rel, "relative_path": rel, "filename": rel.rsplit("/", 1)[-1]},
                "matches":  [{"line": i + 1, "text": f"hit {i + 1}"} for i in range(n)],
            })
        def _fake_post(path, body, timeout=120):
            return 200, {"overflow": False, "found": len(hits),
                         "hits": hits, "facet_counts": []}
        _ms._post = _fake_post

    def test_threshold_reached_at_20(self):
        """Exactly 20 files → tier 2 (filenames-only)."""
        self._install_files_with_matches([(f"src/F{i}.cs", 3) for i in range(20)])
        result = _ms.query_codebase("calls", "Foo")
        # Tier 2 marker: hit-count parens, no path:line: lines for content
        assert "(3 hits)" in result
        # No grep-style line:content body
        assert "src/F0.cs:1:" not in result
        # Suggestion to drill into a single file
        assert "query_single_file" in result
        assert 'file="$SRC_ROOT/' in result

    def test_below_threshold_is_tier3_not_tier2(self):
        """19 files → tier 3 (per-line content shown)."""
        self._install_files_with_matches([(f"src/F{i}.cs", 3) for i in range(19)])
        result = _ms.query_codebase("calls", "Foo")
        # Tier 3 emits path:line: content
        assert "src/F0.cs:1: hit 1" in result

    def test_sorted_by_hit_count_desc(self):
        """Files must be ordered by hit count, highest first."""
        files = [(f"src/F{i}.cs", i + 1) for i in range(25)]  # F0=1 hit, F24=25 hits
        self._install_files_with_matches(files)
        result = _ms.query_codebase("calls", "Foo")
        # The line for F24 (25 hits) must appear before F0 (1 hit).
        idx_high = result.index("src/F24.cs")
        idx_low  = result.index("src/F0.cs")
        assert idx_high < idx_low

    def test_singular_hit_label(self):
        """A file with exactly 1 hit uses 'hit', not 'hits'."""
        files = [(f"src/F{i}.cs", 1) for i in range(20)]
        self._install_files_with_matches(files)
        result = _ms.query_codebase("calls", "Foo")
        assert "(1 hit)" in result
        assert "(1 hits)" not in result

    def test_suggestion_mirrors_call_params(self):
        """The suggested query_single_file must include uses_kind / symbol_kind."""
        self._install_files_with_matches([(f"src/F{i}.cs", 3) for i in range(20)])
        result = _ms.query_codebase("uses", "IRepo", uses_kind="param")
        assert 'uses_kind="param"' in result
        assert '"uses"' in result
        assert '"IRepo"' in result

    def test_header_reports_totals(self):
        files = [(f"src/F{i}.cs", 4) for i in range(20)]
        self._install_files_with_matches(files)
        result = _ms.query_codebase("calls", "Foo")
        assert "files with matches: 20" in result
        assert "total matches: 80" in result


# ── query_codebase tier 3 (per-line content, capped per file) ────────────────

@_skip
class TestQueryCodebaseTier3(unittest.TestCase):
    """Tier 3: <20 files with AST matches → per-line content, ≤10 lines/file."""

    def setUp(self):
        self._orig_post = _ms._post
        self._orig_warn = _ms._queue_warning
        _ms._queue_warning = lambda: ""

    def tearDown(self):
        _ms._post          = self._orig_post
        _ms._queue_warning = self._orig_warn

    def _install(self, file_match_counts):
        hits = []
        for rel, n in file_match_counts:
            hits.append({
                "document": {"id": rel, "relative_path": rel, "filename": rel.rsplit("/", 1)[-1]},
                "matches":  [{"line": i + 1, "text": f"hit {i + 1}"} for i in range(n)],
            })
        def _fake_post(path, body, timeout=120):
            return 200, {"overflow": False, "found": len(hits),
                         "hits": hits, "facet_counts": []}
        _ms._post = _fake_post

    def test_emits_path_line_content(self):
        self._install([("services/Order.cs", 2), ("services/Billing.cs", 1)])
        result = _ms.query_codebase("calls", "Foo")
        assert "services/Order.cs:1: hit 1" in result
        assert "services/Order.cs:2: hit 2" in result
        assert "services/Billing.cs:1: hit 1" in result

    def test_caps_at_10_lines_per_file(self):
        """A file with 25 hits is shown as 10 lines + a suggestion."""
        self._install([("src/Big.cs", 25)])
        result = _ms.query_codebase("calls", "Foo")
        # Lines 1-10 present
        for i in range(1, 11):
            assert f"src/Big.cs:{i}: hit {i}" in result
        # Lines 11-25 absent
        for i in range(11, 26):
            assert f"src/Big.cs:{i}:" not in result
        # Per-file suggestion appended
        assert "25 total hits" in result
        assert "query_single_file" in result
        assert 'file="$SRC_ROOT/src/Big.cs"' in result

    def test_no_suggestion_when_under_cap(self):
        """File with <=10 hits shouldn't trigger a per-file suggestion."""
        self._install([("src/Small.cs", 5)])
        result = _ms.query_codebase("calls", "Foo")
        assert "query_single_file" not in result
        assert "more than" not in result

    def test_files_sorted_by_hit_count(self):
        """In tier 3, content blocks for higher-hit files come first."""
        self._install([("src/Tiny.cs", 1), ("src/Huge.cs", 8), ("src/Mid.cs", 3)])
        result = _ms.query_codebase("calls", "Foo")
        idx_huge = result.index("src/Huge.cs:1:")
        idx_mid  = result.index("src/Mid.cs:1:")
        idx_tiny = result.index("src/Tiny.cs:1:")
        assert idx_huge < idx_mid < idx_tiny

    def test_drops_files_with_zero_ast_matches(self):
        """Index false positives (matches=[]) shouldn't appear."""
        hits = [
            {"document": {"id": "a", "relative_path": "src/Real.cs", "filename": "Real.cs"},
             "matches": [{"line": 1, "text": "hit"}]},
            {"document": {"id": "b", "relative_path": "src/FalsePositive.cs", "filename": "FalsePositive.cs"},
             "matches": []},
        ]
        def _fake(path, body, timeout=120):
            return 200, {"overflow": False, "found": 2, "hits": hits, "facet_counts": []}
        _ms._post = _fake
        result = _ms.query_codebase("calls", "Foo")
        assert "src/Real.cs" in result
        assert "FalsePositive" not in result
        assert "files with matches: 1" in result

    def test_empty_results(self):
        """No files with AST matches → 'No AST matches found.'"""
        self._install([])  # zero files
        result = _ms.query_codebase("calls", "Foo")
        assert "No AST matches found" in result

    def test_multi_file_with_some_capped(self):
        """Mix of capped and uncapped files in tier 3."""
        self._install([("src/Big.cs", 15), ("src/Small.cs", 2)])
        result = _ms.query_codebase("calls", "Foo")
        # Big.cs gets capped at 10 lines + suggestion
        assert "src/Big.cs:1: hit 1" in result
        assert "src/Big.cs:10: hit 10" in result
        assert "src/Big.cs:11:" not in result
        # Small.cs shown in full
        assert "src/Small.cs:1: hit 1" in result
        assert "src/Small.cs:2: hit 2" in result
        # Suggestion only for Big.cs
        assert "src/Big.cs"   in result
        assert "15 total hits" in result


# ── _sync_state ───────────────────────────────────────────────────────────────

@_skip
class TestSyncState(unittest.TestCase):
    """Pure inspection of /status response shape — no I/O."""

    def test_fully_synced(self):
        synced, state = _ms._sync_state({
            "typesense_ok": True,
            "queue":  {"depth": 0},
            "syncer": {"running": False, "pending": 0},
        })
        assert synced is True
        assert "queue empty" in state and "idle" in state

    def test_queue_has_pending(self):
        synced, state = _ms._sync_state({
            "typesense_ok": True,
            "queue":  {"depth": 7},
            "syncer": {"running": False, "pending": 0},
        })
        assert synced is False
        assert "queue=7" in state

    def test_syncer_running(self):
        synced, state = _ms._sync_state({
            "typesense_ok": True,
            "queue":  {"depth": 0},
            "syncer": {"running": True, "pending": 0},
        })
        assert synced is False
        assert "syncer running" in state

    def test_syncer_pending_jobs(self):
        synced, state = _ms._sync_state({
            "typesense_ok": True,
            "queue":  {"depth": 0},
            "syncer": {"running": False, "pending": 3},
        })
        assert synced is False
        assert "syncer pending=3" in state

    def test_queue_and_syncer_both_busy(self):
        synced, state = _ms._sync_state({
            "typesense_ok": True,
            "queue":  {"depth": 4},
            "syncer": {"running": True, "pending": 1},
        })
        assert synced is False
        assert "queue=4" in state
        assert "syncer running" in state
        assert "pending=1" in state

    def test_missing_fields_treated_as_idle(self):
        """A status response with no queue/syncer keys is not 'unsynced' on its own."""
        synced, _ = _ms._sync_state({"typesense_ok": True})
        assert synced is True

    def test_non_dict_input(self):
        synced, state = _ms._sync_state(None)  # type: ignore[arg-type]
        assert synced is False
        assert state


# ── wait_for_sync ─────────────────────────────────────────────────────────────

@_skip
class TestWaitForSync(unittest.TestCase):
    """End-to-end behaviour of wait_for_sync, mocking _get and time.sleep
    so the test does not actually block."""

    def setUp(self):
        # Patch sleep to a no-op + record durations.
        self._orig_sleep = _ms.time.sleep
        self._slept: list[float] = []
        _ms.time.sleep = lambda s: self._slept.append(s)
        # Patch monotonic to a controllable counter.
        self._t = [0.0]
        self._orig_monotonic = _ms.time.monotonic
        _ms.time.monotonic = lambda: self._t[0]
        # Default _resolve_root patch so root validation does not need real config.
        self._orig_resolve_root = _ms._resolve_root
        _ms._resolve_root = lambda name="": _root("default", "C:/myproject/src")
        # Default _get patch slot — each test installs its own.
        self._orig_get = _ms._get

    def tearDown(self):
        _ms.time.sleep      = self._orig_sleep
        _ms.time.monotonic  = self._orig_monotonic
        _ms._get            = self._orig_get
        _ms._resolve_root   = self._orig_resolve_root

    def _set_responses(self, responses):
        """Patch _ms._get to return successive responses, advancing time by
        poll_interval each call so the loop terminates deterministically."""
        it = iter(responses)
        def _fake_get(path, timeout=10):
            self._t[0] += 0.5  # simulate poll cadence
            return next(it)
        _ms._get = _fake_get

    def test_already_synced_first_poll(self):
        self._set_responses([(200, {"typesense_ok": True,
                                    "queue": {"depth": 0},
                                    "syncer": {"running": False, "pending": 0}})])
        result = _ms.wait_for_sync(timeout_s=5)
        assert "Index synced" in result
        # Initial 1s warm-up sleep must happen before any poll.
        assert self._slept and self._slept[0] == 1.0

    def test_synced_after_drain(self):
        """Queue=3 then 0 — should report it was busy at first."""
        self._set_responses([
            (200, {"typesense_ok": True, "queue": {"depth": 3}, "syncer": {"running": False}}),
            (200, {"typesense_ok": True, "queue": {"depth": 1}, "syncer": {"running": False}}),
            (200, {"typesense_ok": True, "queue": {"depth": 0}, "syncer": {"running": False}}),
        ])
        result = _ms.wait_for_sync(timeout_s=10)
        assert "Index synced" in result
        assert "was: queue=3" in result

    def test_timeout_with_remaining_work(self):
        """All polls return queue=5 — must time out and surface what's pending."""
        self._set_responses([
            (200, {"typesense_ok": True, "queue": {"depth": 5}, "syncer": {"running": False}})
        ] * 50)
        result = _ms.wait_for_sync(timeout_s=2)
        assert "Timed out" in result
        assert "queue=5" in result
        assert "verify_index" in result  # hint included

    def test_unreachable_indexserver(self):
        """Persistent _get failures across the deadline → 'unreachable' message.

        wait_for_sync retries transient errors (a slow /status round-trip is
        not the same as a dead daemon), so the loop must run until our deadline
        elapses before reporting unreachable.
        """
        def _boom(path, timeout=10):
            self._t[0] += 0.5  # advance clock so the deadline is reachable
            raise OSError("connection refused")
        _ms._get = _boom
        result = _ms.wait_for_sync(timeout_s=2)
        assert "unreachable" in result.lower(), f"got: {result!r}"
        assert "ts start" in result
        # The old "Indexserver is NOT running" phrasing was misleading because
        # it fired on the first transient blip — must not appear anymore.
        assert "Indexserver is NOT running" not in result

    def test_transient_error_retried_then_succeeds(self):
        """A single /status timeout must NOT be reported as 'not running' —
        wait_for_sync should retry and surface a successful sync on the next poll.
        Regression test for the user-reported bug where a slow backend
        round-trip in /status caused an immediate 'NOT running' return."""
        calls = {"n": 0}
        def _flaky(path, timeout=10):
            self._t[0] += 0.5
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return (200, {"typesense_ok": True,
                          "queue":  {"depth": 0},
                          "syncer": {"running": False, "pending": 0}})
        _ms._get = _flaky
        result = _ms.wait_for_sync(timeout_s=10)
        assert "Index synced" in result, f"expected sync after retry, got: {result!r}"
        assert calls["n"] >= 2, "must have retried after the first failure"

    def test_status_http_error_returned(self):
        self._set_responses([(503, {"error": "loading"})])
        result = _ms.wait_for_sync(timeout_s=5)
        assert "Status check failed" in result
        assert "503" in result

    def test_unknown_root_rejected_without_polling(self):
        def _should_not_be_called(*_a, **_kw):
            raise AssertionError("_get must not be called when root is invalid")
        _ms._get          = _should_not_be_called
        _ms._resolve_root = lambda name="": (_ for _ in ()).throw(ValueError(f"Unknown root: {name!r}"))
        result = _ms.wait_for_sync(timeout_s=5, root="nope")
        assert "Error:" in result
        assert "nope" in result

    def test_zero_timeout_skips_initial_delay(self):
        """timeout_s=0 should not make us sleep a full second up front."""
        self._set_responses([
            (200, {"typesense_ok": True, "queue": {"depth": 0}, "syncer": {"running": False}})
        ])
        _ms.wait_for_sync(timeout_s=0)
        assert all(s <= 0.0 for s in self._slept[:1])


if __name__ == "__main__":
    unittest.main()
