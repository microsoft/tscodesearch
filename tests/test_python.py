"""
Tests for Python support: extract_metadata, process_py_file, and Typesense indexing.

TestExtractPyMetadata and TestQueryPy require no server.
TestPySemanticFields requires Typesense to be running.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest codesearch/tests/test_python.py -v
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _assert_server_ok, _search, _delete_collection, _make_git_repo,
    _FOO_PY, _BAR_PY,
)
from indexserver.indexer import run_index, extract_metadata
from query.dispatch import query_file as _query_file


# ── TestExtractPyMetadata ─────────────────────────────────────────────────────

class TestExtractPyMetadata(unittest.TestCase):
    """Unit tests for extract_metadata — no server needed."""

    def _meta(self, src):
        return extract_metadata(src.encode(), ".py")

    def test_class_names(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("Foo", meta["class_names"])

    def test_class_names_multiple(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("IFoo", meta["class_names"])

    def test_base_types_interface(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("IFoo", meta["base_types"])

    def test_base_types_multiple(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("IComparable", meta["base_types"])

    def test_base_types_subclass(self):
        meta = self._meta(_BAR_PY)
        self.assertIn("Foo", meta["base_types"])

    def test_method_names(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("process", meta["method_names"])

    def test_method_names_multiple(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("compute", meta["method_names"])

    def test_member_sigs_contains_function_name(self):
        meta = self._meta(_FOO_PY)
        sigs = meta["member_sigs"]
        self.assertTrue(any("process" in s for s in sigs), f"member_sigs: {sigs}")

    def test_member_sigs_include_return_type(self):
        meta = self._meta(_FOO_PY)
        sigs = meta["member_sigs"]
        self.assertTrue(any("Optional" in s for s in sigs), f"member_sigs: {sigs}")

    def test_call_sites(self):
        meta = self._meta(_BAR_PY)
        self.assertIn("process", meta["call_sites"])

    def test_decorators_in_attr_names(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("dataclass", meta["attr_names"])

    def test_imports_in_usings(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("os", meta["usings"])

    def test_from_imports_in_usings(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("typing", meta["usings"])

    def test_from_imports_top_level_module(self):
        meta = self._meta(_BAR_PY)
        self.assertIn("myapp", meta["usings"])

    def test_type_refs_from_annotations(self):
        meta = self._meta(_FOO_PY)
        self.assertIn("Optional", meta["type_refs"])

    def test_namespace_empty(self):
        meta = self._meta(_FOO_PY)
        self.assertEqual(meta["namespace"], "")


# ── TestQueryPy ───────────────────────────────────────────────────────────────

class TestQueryPy(unittest.TestCase):
    """Unit tests for process_py_file() — no server needed."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_qpy_test_")
        cls.foo_path = os.path.join(cls.tmpdir, "foo.py")
        cls.bar_path = os.path.join(cls.tmpdir, "bar.py")
        with open(cls.foo_path, "w", encoding="utf-8") as f:
            f.write(_FOO_PY)
        with open(cls.bar_path, "w", encoding="utf-8") as f:
            f.write(_BAR_PY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, path, mode, mode_arg=None):
        with open(path, "rb") as _f:
            src_bytes = _f.read()
        matches = _query_file(src_bytes, ".py", mode, mode_arg or "")
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
        self.assertIn("IFoo", out)

    def test_classes_multiple_bases(self):
        n, out = self._run(self.foo_path, "classes")
        self.assertIn("IComparable", out)

    # ── mode: methods ──────────────────────────────────────────────────────────

    def test_methods_lists_process(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertGreater(n, 0)
        self.assertIn("process", out)

    def test_methods_lists_compute(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("compute", out)

    def test_methods_shows_class_context(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("[in Foo]", out)

    def test_methods_shows_return_type(self):
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("Optional", out)

    # ── mode: calls ───────────────────────────────────────────────────────────

    def test_calls_found_in_bar(self):
        n, out = self._run(self.bar_path, "calls", "process")
        self.assertGreater(n, 0)
        self.assertIn("process", out)

    def test_calls_absent_method_no_match(self):
        n, out = self._run(self.foo_path, "calls", "nonexistent_function_xyz")
        self.assertEqual(n, 0)

    # ── mode: implements ──────────────────────────────────────────────────────

    def test_implements_ifoo_finds_foo(self):
        n, out = self._run(self.foo_path, "implements", "IFoo")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    def test_implements_foo_finds_bar(self):
        n, out = self._run(self.bar_path, "implements", "Foo")
        self.assertGreater(n, 0)
        self.assertIn("Bar", out)

    def test_implements_nonexistent_no_match(self):
        n, out = self._run(self.foo_path, "implements", "INonExistent999")
        self.assertEqual(n, 0)

    # ── mode: ident ───────────────────────────────────────────────────────────

    def test_ident_finds_foo(self):
        n, out = self._run(self.foo_path, "ident", "Foo")
        self.assertGreater(n, 0)
        self.assertIn("Foo", out)

    def test_ident_absent_no_match(self):
        n, out = self._run(self.foo_path, "ident", "ZZZNonExistentXXX")
        self.assertEqual(n, 0)

    # ── mode: declarations ────────────────────────────────────────────────────

    def test_find_returns_source(self):
        n, out = self._run(self.foo_path, "declarations", "process")
        self.assertGreater(n, 0)
        self.assertIn("def process", out)

    def test_find_class_returns_full_body(self):
        n, out = self._run(self.foo_path, "declarations", "Foo")
        self.assertGreater(n, 0)
        self.assertIn("class Foo", out)

    # ── mode: decorators ──────────────────────────────────────────────────────

    def test_decorators_found(self):
        n, out = self._run(self.foo_path, "decorators")
        self.assertGreater(n, 0)
        self.assertIn("dataclass", out)

    def test_decorators_filtered_by_name(self):
        n, out = self._run(self.foo_path, "decorators", "dataclass")
        self.assertGreater(n, 0)
        self.assertIn("dataclass", out)

    def test_decorators_filter_no_match(self):
        n, out = self._run(self.foo_path, "decorators", "nonexistent_decorator_xyz")
        self.assertEqual(n, 0)

    # ── mode: imports ─────────────────────────────────────────────────────────

    def test_imports_found(self):
        n, out = self._run(self.foo_path, "imports")
        self.assertGreater(n, 0)
        self.assertIn("import", out)

    def test_imports_shows_os(self):
        n, out = self._run(self.foo_path, "imports")
        self.assertIn("os", out)

    def test_imports_shows_from_import(self):
        n, out = self._run(self.foo_path, "imports")
        self.assertIn("typing", out)

    def test_imports_includes_future_import(self):
        # from __future__ import annotations is a future_import_statement node,
        # distinct from import_from_statement — must be found too
        n, out = self._run(self.foo_path, "imports")
        self.assertIn("__future__", out)

    def test_imports_future_appears_first(self):
        # future import must be line 1 of the fixture
        n, out = self._run(self.foo_path, "imports")
        first_line = out.strip().splitlines()[0]
        self.assertIn("__future__", first_line)

    # ── mode: attrs (Python decorator alias) ──────────────────────────────────

    def test_attrs_mode_works_for_python(self):
        # --attrs in query_util maps to mode "attrs"; Python dispatch must
        # alias it to decorators rather than returning Unknown mode
        n, out = self._run(self.foo_path, "attrs")
        self.assertGreater(n, 0)
        self.assertIn("dataclass", out)

    def test_attrs_mode_filtered(self):
        n, out = self._run(self.foo_path, "attrs", "dataclass")
        self.assertGreater(n, 0)
        self.assertIn("dataclass", out)

    def test_attrs_mode_no_match(self):
        n, out = self._run(self.foo_path, "attrs", "nonexistent_decorator_xyz")
        self.assertEqual(n, 0)

    # ── mode: params ──────────────────────────────────────────────────────────

    def test_params_found(self):
        n, out = self._run(self.foo_path, "params", "process")
        self.assertGreater(n, 0)

    def test_params_shows_types(self):
        n, out = self._run(self.foo_path, "params", "process")
        self.assertIn("str", out)

    def test_params_typed_args_shows_name(self):
        # *args: str must show "*args: str", not just ": str"
        # (list_splat_pattern wraps the identifier inside typed_parameter)
        n, out = self._run(self.foo_path, "params", "variadic")
        self.assertGreater(n, 0)
        self.assertIn("*args", out)
        self.assertIn("**kwargs", out)

    def test_params_typed_args_shows_type(self):
        n, out = self._run(self.foo_path, "params", "variadic")
        self.assertIn("*args: str", out)
        self.assertIn("**kwargs: int", out)

    def test_params_keyword_only_separator(self):
        # bare * in parameter list (positional_separator) must appear
        n, out = self._run(self.foo_path, "params", "kw_only")
        self.assertGreater(n, 0)
        self.assertIn("*", out)
        self.assertIn("debug", out)

    # ── relative path display ─────────────────────────────────────────────────

    def test_display_path_is_relative(self):
        n, out = self._run(self.foo_path, "classes")
        self.assertGreater(n, 0)
        self.assertIn("foo.py", out)
        tmpdir_norm = self.tmpdir.replace("\\", "/")
        self.assertNotIn(tmpdir_norm, out)

    # ── consistency: process_py_file ↔ extract_metadata ───────────────────

    def test_class_names_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        self.assertIn("Foo", meta["class_names"])
        n, out = self._run(self.foo_path, "classes")
        self.assertIn("Foo", out)

    def test_base_types_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        self.assertIn("IFoo", meta["base_types"])
        n, out = self._run(self.foo_path, "implements", "IFoo")
        self.assertGreater(n, 0)

    def test_method_names_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        self.assertIn("process", meta["method_names"])
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("process", out)

    def test_call_sites_consistent(self):
        meta = extract_metadata(_BAR_PY.encode(), ".py")
        self.assertIn("process", meta["call_sites"])
        n, out = self._run(self.bar_path, "calls", "process")
        self.assertGreater(n, 0)

    def test_decorators_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        self.assertIn("dataclass", meta["attr_names"])
        n, out = self._run(self.foo_path, "decorators", "dataclass")
        self.assertGreater(n, 0)

    def test_imports_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        self.assertIn("os", meta["usings"])
        n, out = self._run(self.foo_path, "imports")
        self.assertIn("os", out)

    def test_member_sigs_consistent(self):
        meta = extract_metadata(_FOO_PY.encode(), ".py")
        sigs = meta["member_sigs"]
        self.assertTrue(any("process" in s for s in sigs), f"member_sigs: {sigs}")
        n, out = self._run(self.foo_path, "methods")
        self.assertIn("process", out)


# ── TestPySemanticFields ──────────────────────────────────────────────────────

class TestPySemanticFields(unittest.TestCase):
    """Verify that Python files get their semantic fields indexed by Typesense."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_pysem_{stamp}"
        cls.tmpdir = _make_git_repo({
            "myapp/foo.py": _FOO_PY,
            "myapp/bar.py": _BAR_PY,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, filename):
        hits = _search(self.coll, os.path.splitext(filename)[0],
                       query_by="filename,class_names,method_names,tokens")
        return next((h for h in hits if h["filename"] == filename), None)

    def test_py_class_names_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("Foo", foo.get("class_names", []))

    def test_py_method_names_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("process", foo.get("method_names", []))

    def test_py_base_types_indexed(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("IFoo", foo.get("base_types", []))

    def test_py_subclass_base_types_indexed(self):
        bar = self._get("bar.py")
        self.assertIsNotNone(bar)
        self.assertIn("Foo", bar.get("base_types", []))

    def test_py_call_sites_indexed(self):
        bar = self._get("bar.py")
        self.assertIsNotNone(bar)
        self.assertIn("process", bar.get("call_sites", []))

    def test_py_decorators_in_attr_names(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("dataclass", foo.get("attr_names", []))

    def test_py_imports_in_usings(self):
        foo = self._get("foo.py")
        self.assertIsNotNone(foo)
        self.assertIn("os", foo.get("usings", []))

    def test_py_base_types_searchable_via_typesense(self):
        hits = _search(self.coll, "IFoo", query_by="base_types,class_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("foo.py", names)

    def test_py_call_sites_searchable_via_typesense(self):
        hits = _search(self.coll, "process", query_by="call_sites,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("bar.py", names)

    def test_py_member_sigs_searchable_via_typesense(self):
        hits = _search(self.coll, "process", query_by="member_sigs,method_names,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("foo.py", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
