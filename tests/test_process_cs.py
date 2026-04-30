"""
Tests for the C# structural query API — query_file() / query_cs_bytes() (no server needed).

Verifies each query mode and consistency between query/ and indexer.py.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest tests/test_process_cs.py -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _FOO_CS, _BAR_CS, _QUALIFIED_CS, _GENERIC_WRAPPER_CS, _BLOBSTORE_CS,
)
from indexserver.api import _run_query
from indexserver.indexer import extract_metadata
import query.dispatch as _q
from query.cs import (
    _cs_parser,
    q_all_refs, q_calls, q_methods, q_implements, q_uses,
)


class TestQueryCs(unittest.TestCase):
    """Unit tests for process_cs_file() from query — no server needed.

    Verifies:
    1. Each query mode extracts the expected output from sample C# files.
    2. The AST fields query.py extracts are consistent with what indexer.py indexes.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_qcs_test_")
        cls.foo_path = os.path.join(cls.tmpdir, "Foo.cs")
        cls.bar_path = os.path.join(cls.tmpdir, "Bar.cs")
        with open(cls.foo_path, "w", encoding="utf-8") as f:
            f.write(_FOO_CS)
        with open(cls.bar_path, "w", encoding="utf-8") as f:
            f.write(_BAR_CS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, path, mode, mode_arg=None, uses_kind=None):
        with open(path, "rb") as _f:
            src_bytes = _f.read()
        matches = _q.query_file(src_bytes, ".cs", mode, mode_arg or "",
                                uses_kind=uses_kind)
        path_norm = path.replace("\\", "/")
        root_norm = self.tmpdir.replace("\\", "/").rstrip("/")
        disp = (path_norm[len(root_norm) + 1:]
                if path_norm.lower().startswith(root_norm.lower() + "/")
                else path_norm)
        out = "\n".join(f"{disp}:{m['line']}: {m['text']}" for m in (matches or []))
        return len(matches or []), out

    # ── mode: classes ──────────────────────────────────────────────────────────

    def test_classes_lists_foo(self):
        n, out = self._run(self.foo_path, "classes")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    def test_classes_shows_base_types(self):
        n, out = self._run(self.foo_path, "classes")
        self.assertIn("IDisposable", out)

    # ── mode: methods ──────────────────────────────────────────────────────────

    def test_methods_lists_dispose(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertGreater(n, 0)
        self.assertIn("Dispose", out)

    def test_methods_lists_dowork(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("DoWork", out)

    def test_methods_lists_field(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("Name", out)

    # ── mode: fields ──────────────────────────────────────────────────────────

    def test_fields_lists_foo_field_in_bar(self):
        n, out = self._run(self.bar_path, "fields")
        self.assertGreater(n, 0)
        self.assertIn("_foo", out)

    # ── mode: calls ───────────────────────────────────────────────────────────

    def test_calls_dowork_found_in_bar(self):
        n, out = self._run(self.bar_path, "calls", "DoWork")
        self.assertGreater(n, 0)
        self.assertIn("DoWork", out)

    def test_calls_absent_method_no_match(self):
        n, out = self._run(self.foo_path, "calls", "NonExistentMethod999")
        self.assertEqual(n, 0)

    # ── mode: implements ──────────────────────────────────────────────────────

    def test_implements_idisposable_finds_foo(self):
        n, out = self._run(self.foo_path, "implements", "IDisposable")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    def test_implements_nonexistent_no_match(self):
        n, out = self._run(self.foo_path, "implements", "INonExistent999")
        self.assertEqual(n, 0)

    # ── mode: uses ────────────────────────────────────────────────────────────

    def test_uses_foo_in_bar(self):
        n, out = self._run(self.bar_path, "uses", "Foo")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    # ── mode: uses(kind=field) ────────────────────────────────────────────────

    def test_field_type_foo_in_bar(self):
        n, out = self._run(self.bar_path, "uses", "Foo", uses_kind="field")
        self.assertGreater(n, 0)
        self.assertIn("_foo", out)

    # ── mode: uses(kind=param) ────────────────────────────────────────────────

    def test_param_type_foo_in_bar_ctor(self):
        n, out = self._run(self.bar_path, "uses", "Foo", uses_kind="param")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    # ── mode: attrs ───────────────────────────────────────────────────────────

    def test_attrs_serializable_in_foo(self):
        n, out = self._run(self.foo_path, "attrs", "Serializable")
        self.assertGreater(n, 0)
        self.assertIn("Serializable", out)

    # ── mode: usings ──────────────────────────────────────────────────────────

    def test_usings_system_in_foo(self):
        n, out = self._run(self.foo_path, "usings")
        self.assertGreater(n, 0)
        self.assertIn("System", out)

    # ── relative path stripping ───────────────────────────────────────────────

    def test_display_path_is_relative(self):
        n, out = self._run(self.foo_path, "classes")
        self.assertGreater(n, 0)
        self.assertIn("Foo.cs", out)
        tmpdir_norm = self.tmpdir.replace("\\", "/")
        self.assertNotIn(tmpdir_norm, out,
                         f"full tmpdir path leaked into output:\n{out}")

    # ── consistency: query.py ↔ indexer.py ───────────────────────────────────

    def test_class_names_consistent(self):
        meta = extract_metadata(_FOO_CS.encode(), ".cs")
        self.assertIn("Foo", meta["class_names"])
        n, out = self._run(self.foo_path, "classes")
        self.assertIn("Foo", out)

    def test_member_sigs_consistent(self):
        meta = extract_metadata(_FOO_CS.encode(), ".cs")
        sigs = meta["member_sigs"]
        self.assertTrue(any("Dispose" in s for s in sigs), f"member_sigs: {sigs}")
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("Dispose", out)
        self.assertIn("DoWork", out)

    def test_base_types_consistent(self):
        meta = extract_metadata(_FOO_CS.encode(), ".cs")
        self.assertIn("IDisposable", meta["base_types"])
        n, out = self._run(self.foo_path, "implements", "IDisposable")
        self.assertGreater(n, 0)

    def test_call_sites_consistent(self):
        meta = extract_metadata(_BAR_CS.encode(), ".cs")
        self.assertIn("DoWork", meta["call_sites"])
        n, out = self._run(self.bar_path, "calls", "DoWork")
        self.assertGreater(n, 0)

    def test_type_refs_consistent(self):
        meta = extract_metadata(_BAR_CS.encode(), ".cs")
        self.assertIn("Foo", meta["type_refs"])
        n, out = self._run(self.bar_path, "uses", "Foo", uses_kind="field")
        self.assertGreater(n, 0)

    def test_attr_names_consistent(self):
        meta = extract_metadata(_FOO_CS.encode(), ".cs")
        self.assertIn("Serializable", meta["attr_names"])
        n, out = self._run(self.foo_path, "attrs", "Serializable")
        self.assertGreater(n, 0)

    def test_usings_consistent(self):
        meta = extract_metadata(_FOO_CS.encode(), ".cs")
        self.assertIn("System", meta["usings"])
        n, out = self._run(self.foo_path, "usings")
        self.assertIn("System", out)

    # ── qualified-name stripping ──────────────────────────────────────────────

    @classmethod
    def _make_qualified_file(cls):
        path = os.path.join(cls.tmpdir, "Qualified.cs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_QUALIFIED_CS)
        return path

    def test_implements_qualified_name_matches_simple(self):
        path = self._make_qualified_file()
        n, out = self._run(path, "implements", "IBlobStore")
        self.assertGreater(n, 0)

    def test_field_type_qualified_matches_simple(self):
        path = self._make_qualified_file()
        n, out = self._run(path, "uses", "IBlobStore", uses_kind="field")
        self.assertGreater(n, 0)

    def test_param_type_qualified_matches_simple(self):
        path = self._make_qualified_file()
        n, out = self._run(path, "uses", "IBlobStore", uses_kind="param")
        self.assertGreater(n, 0)

    def test_attrs_qualified_matches_simple(self):
        path = self._make_qualified_file()
        n, out = self._run(path, "attrs", "Authorize")
        self.assertGreater(n, 0)

    # ── generic wrapper matching ──────────────────────────────────────────────

    @classmethod
    def _make_generic_wrapper_file(cls):
        path = os.path.join(cls.tmpdir, "GenericWrapper.cs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_GENERIC_WRAPPER_CS)
        return path

    def test_field_type_matches_generic_wrapper_arg(self):
        path = self._make_generic_wrapper_file()
        n, out = self._run(path, "uses", "IBlobStore", uses_kind="field")
        self.assertGreater(n, 0)

    def test_param_type_matches_generic_wrapper_arg(self):
        path = self._make_generic_wrapper_file()
        n, out = self._run(path, "uses", "IBlobStore", uses_kind="param")
        self.assertGreater(n, 0)

    def test_field_type_outer_still_matches(self):
        path = self._make_generic_wrapper_file()
        n, out = self._run(path, "uses", "IList", uses_kind="field")
        self.assertGreater(n, 0)


class TestQueryApi(unittest.TestCase):
    """Tests that _run_query (the POST /query handler) returns results
    identical to calling the underlying tree-sitter functions directly.

    This ensures the HTTP API behaves exactly like the MCP query_ast tool,
    which calls the same functions without the HTTP layer.

    No server required — all tests run against temp files with synthetic fixtures.
    """

    @classmethod
    def setUpClass(cls):
        tmpdir = Path(tempfile.mkdtemp(prefix="ts_qapi_test_"))
        cls.tmpdir = tmpdir
        cls.foo_path = tmpdir / "Foo.cs"
        cls.bar_path = tmpdir / "Bar.cs"
        cls.blob_path = tmpdir / "BlobStore.cs"
        cls.generic_path = tmpdir / "GenericWrapper.cs"
        for path, src in [
            (cls.foo_path, _FOO_CS),
            (cls.bar_path, _BAR_CS),
            (cls.blob_path, _BLOBSTORE_CS),
            (cls.generic_path, _GENERIC_WRAPPER_CS),
        ]:
            path.write_text(src, encoding="utf-8")
    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _direct(self, path, fn):
        """Call a query function directly (as query_ast MCP tool does)."""
        with open(path, "rb") as _f:
            src = _f.read()
        tree = _cs_parser.parse(src)
        lines = src.decode("utf-8", errors="replace").splitlines()
        return fn(src, tree, lines)

    # ── result structure ───────────────────────────────────────────────────────

    def test_returns_list(self):
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        self.assertIsInstance(result, list)

    def test_result_has_file_and_matches_keys(self):
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        self.assertEqual(len(result), 1)
        self.assertIn("file", result[0])
        self.assertIn("matches", result[0])

    def test_match_has_line_and_text_keys(self):
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        m = result[0]["matches"][0]
        self.assertIn("line", m)
        self.assertIn("text", m)

    def test_line_is_1_indexed(self):
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        for m in result[0]["matches"]:
            self.assertGreaterEqual(m["line"], 1)

    def test_original_path_preserved(self):
        """File path in result is the resolved string form of the input Path."""
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        self.assertEqual(result[0]["file"], str(self.generic_path.resolve()))

    # ── ident mode vs direct call ──────────────────────────────────────────────

    def test_ident_matches_direct_q_all_refs(self):
        """_run_query('all_refs', ...) returns same (line, text) pairs as q_all_refs directly."""
        direct = self._direct(self.generic_path,
                              lambda s, t, l: q_all_refs(s, t, l, "IBlobStore"))
        via_api = _run_query("all_refs", "IBlobStore", [self.generic_path])
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    def test_ident_finds_all_occurrences(self):
        """ident mode finds every occurrence, not just method signatures."""
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        texts = [m["text"] for m in result[0]["matches"]]
        # _GENERIC_WRAPPER_CS uses IBlobStore in field, property, method return type, param
        self.assertGreater(len(texts), 1)
        self.assertTrue(any("IBlobStore" in t for t in texts))

    def test_ident_generic_wrapper_found_in_sig(self):
        """ident mode finds IBlobStore even when wrapped in a generic type like IList<IBlobStore>."""
        result = _run_query("all_refs", "IBlobStore", [self.generic_path])
        texts = [m["text"] for m in result[0]["matches"]]
        self.assertTrue(any("IBlobStore" in t for t in texts))

    def test_ident_no_match_returns_empty(self):
        result = _run_query("all_refs", "NonExistentType999", [self.foo_path])
        self.assertEqual(result, [])

    # ── calls mode vs direct call ─────────────────────────────────────────────

    def test_calls_matches_direct_q_calls(self):
        """_run_query('calls', 'DoWork', ...) matches q_calls directly."""
        direct = self._direct(self.bar_path,
                              lambda s, t, l: q_calls(s, t, l, "DoWork"))
        via_api = _run_query("calls", "DoWork", [self.bar_path])
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    def test_calls_finds_dowork_in_bar(self):
        result = _run_query("calls", "DoWork", [self.bar_path])
        self.assertEqual(len(result), 1)
        self.assertTrue(any("DoWork" in m["text"] for m in result[0]["matches"]))

    def test_calls_absent_method_returns_empty(self):
        result = _run_query("calls", "NoSuchMethod999", [self.bar_path])
        self.assertEqual(result, [])

    # ── methods mode vs direct call ───────────────────────────────────────────

    def test_methods_matches_direct_q_methods(self):
        """_run_query('methods', ...) returns same pairs as q_methods directly."""
        direct = self._direct(self.foo_path, q_methods)
        via_api = _run_query("methods", "", [self.foo_path])
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    # ── implements mode ───────────────────────────────────────────────────────

    def test_implements_matches_direct(self):
        direct = self._direct(self.foo_path,
                              lambda s, t, l: q_implements(s, t, l, "IDisposable"))
        via_api = _run_query("implements", "IDisposable", [self.foo_path])
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    # ── field_type mode ───────────────────────────────────────────────────────

    def test_field_type_matches_direct(self):
        direct = self._direct(self.generic_path,
                              lambda s, t, l: q_uses(s, t, l, "IBlobStore", uses_kind="field"))
        via_api = _run_query("uses", "IBlobStore", [self.generic_path], uses_kind="field")
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    # ── param_type mode ───────────────────────────────────────────────────────

    def test_param_type_matches_direct(self):
        direct = self._direct(self.generic_path,
                              lambda s, t, l: q_uses(s, t, l, "IBlobStore", uses_kind="param"))
        via_api = _run_query("uses", "IBlobStore", [self.generic_path], uses_kind="param")
        api_pairs = [(m["line"], m["text"]) for m in via_api[0]["matches"]]
        self.assertEqual(api_pairs, list(direct))

    # ── multiple files ────────────────────────────────────────────────────────

    def test_multiple_files_results_ordered_by_input(self):
        result = _run_query("all_refs", "IBlobStore",
                            [self.generic_path, self.blob_path])
        paths = [r["file"] for r in result]
        self.assertIn(str(self.generic_path.resolve()), paths)
        self.assertIn(str(self.blob_path.resolve()), paths)

    def test_multiple_files_only_matching_files_in_result(self):
        result = _run_query("all_refs", "IBlobStore",
                            [self.foo_path, self.generic_path])
        paths = [r["file"] for r in result]
        self.assertNotIn(str(self.foo_path.resolve()), paths)  # Foo.cs has no IBlobStore
        self.assertIn(str(self.generic_path.resolve()), paths)

    # ── error handling ────────────────────────────────────────────────────────

    def test_unknown_mode_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _run_query("bad_mode_xyz", "x", [self.foo_path])
        self.assertIn("bad_mode_xyz", str(ctx.exception))

    def test_missing_file_is_silently_skipped(self):
        result = _run_query("all_refs", "IBlobStore",
                            [Path("/tmp/does_not_exist_999.cs")])
        self.assertEqual(result, [])

    def test_missing_file_does_not_prevent_other_results(self):
        result = _run_query("all_refs", "IBlobStore",
                            [Path("/tmp/does_not_exist_999.cs"), self.generic_path])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["file"], str(self.generic_path.resolve()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
