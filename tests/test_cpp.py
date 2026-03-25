"""
Tests for C/C++ support: extract_cpp_metadata and query_cpp functions.

No server needed — all tests run against tests/query_fixture.cpp.

Run from WSL:
    ~/.local/indexserver-venv/bin/pytest tests/test_cpp.py -v
"""

import os
import sys
import io
import shutil
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser
    _CPP_AVAILABLE = True
except ImportError:
    _CPP_AVAILABLE = False

_SKIP = not _CPP_AVAILABLE
_SKIP_MSG = "tree-sitter-cpp not installed"

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "query_fixture.cpp")


def _setup_parser():
    lang = Language(tscpp.language())
    parser = Parser(lang)
    src = open(FIXTURE_PATH, "rb").read()
    tree = parser.parse(src)
    lines = src.decode("utf-8", errors="replace").splitlines()
    return src, tree, lines


def has(results, sub):
    return any(sub in t for _, t in results)


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestExtractCppMetadata(unittest.TestCase):
    """Unit tests for extract_cpp_metadata — no server needed."""

    @classmethod
    def setUpClass(cls):
        from indexserver.indexer import extract_cpp_metadata
        cls._meta = extract_cpp_metadata(open(FIXTURE_PATH, "rb").read())

    def test_class_names_indexed(self):
        self.assertIn("TextProcessor", self._meta["class_names"])

    def test_struct_names_indexed(self):
        self.assertIn("ProcessResult", self._meta["class_names"])

    def test_base_types_indexed(self):
        self.assertIn("BaseProcessor", self._meta["base_types"])

    def test_function_names_indexed(self):
        self.assertIn("createProcessor", self._meta["method_names"])

    def test_call_sites_indexed(self):
        self.assertIn("process", self._meta["call_sites"])

    def test_includes_in_usings(self):
        self.assertIn("string", self._meta["usings"])

    def test_member_sigs_indexed(self):
        sigs = self._meta["member_sigs"]
        self.assertTrue(any("createProcessor" in s for s in sigs), f"sigs={sigs}")


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestQueryCpp(unittest.TestCase):
    """Unit tests for C++ query functions."""

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _setup_parser()

    def _fx(self):
        return self.src, self.tree, self.lines

    # ── classes ──────────────────────────────────────────────────────────────

    def test_classes_finds_class(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(has(r, "TextProcessor"))

    def test_classes_finds_struct(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(has(r, "ProcessResult"))

    def test_classes_shows_base(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        match = next((t for _, t in r if "TextProcessor" in t), None)
        self.assertIsNotNone(match)
        self.assertIn("BaseProcessor", match)

    def test_classes_kind_tagged(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(any("[class]" in t for _, t in r))

    # ── methods ──────────────────────────────────────────────────────────────

    def test_methods_finds_function(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "createProcessor"))

    def test_methods_finds_member_function(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "process"))

    # ── calls ─────────────────────────────────────────────────────────────────

    def test_calls_finds_function_call(self):
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)

    def test_calls_finds_method_call(self):
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "process")
        self.assertGreater(len(r), 0)

    def test_calls_absent_no_match(self):
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "nonexistentXYZ")
        self.assertEqual(len(r), 0)

    # ── implements ──────────────────────────────────────────────────────────

    def test_implements_finds_derived_class(self):
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "BaseProcessor")
        self.assertGreater(len(r), 0)
        self.assertTrue(has(r, "TextProcessor"))

    def test_implements_interface(self):
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "IProcessor")
        self.assertGreater(len(r), 0)

    def test_implements_nonexistent_no_match(self):
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "INonExistent999")
        self.assertEqual(len(r), 0)

    # ── declarations ────────────────────────────────────────────────────────

    def test_declarations_finds_class(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "TextProcessor")
        self.assertGreater(len(r), 0)

    def test_declarations_finds_function(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)

    def test_declarations_nonexistent_no_match(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "ZZZNonExistentXXX")
        self.assertEqual(len(r), 0)

    # ── all_refs ────────────────────────────────────────────────────────────

    def test_all_refs_finds_type(self):
        from src.query.cpp import cpp_q_all_refs
        r = cpp_q_all_refs(*self._fx(), "TextProcessor")
        self.assertGreater(len(r), 0)

    def test_all_refs_absent_no_match(self):
        from src.query.cpp import cpp_q_all_refs
        r = cpp_q_all_refs(*self._fx(), "ZZZNonExistentXXX")
        self.assertEqual(len(r), 0)

    # ── includes ────────────────────────────────────────────────────────────

    def test_includes_found(self):
        from src.query.cpp import cpp_q_includes
        r = cpp_q_includes(*self._fx())
        self.assertGreater(len(r), 0)
        self.assertTrue(any("#include" in t for _, t in r))

    def test_includes_contains_string(self):
        from src.query.cpp import cpp_q_includes
        r = cpp_q_includes(*self._fx())
        self.assertTrue(any("<string>" in t for _, t in r))

    # ── params ──────────────────────────────────────────────────────────────

    def test_params_found(self):
        from src.query.cpp import cpp_q_params
        r = cpp_q_params(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)

    def test_params_absent_no_match(self):
        from src.query.cpp import cpp_q_params
        r = cpp_q_params(*self._fx(), "nonexistent_xyz")
        self.assertEqual(len(r), 0)


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestProcessCppFile(unittest.TestCase):
    """Tests for process_cpp_file — uses actual file I/O."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_cpp_test_")
        cls.path = os.path.join(cls.tmpdir, "fixture.cpp")
        shutil.copy(FIXTURE_PATH, cls.path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, mode, mode_arg=None):
        from src.query.dispatch import process_cpp_file
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            n = process_cpp_file(
                path=self.path, mode=mode, mode_arg=mode_arg,
                show_path=True, count_only=False, context=0,
                src_root=self.tmpdir,
            )
        finally:
            sys.stdout = old
        return n or 0, buf.getvalue()

    def test_classes_output(self):
        n, out = self._run("classes")
        self.assertGreater(n, 0)
        self.assertIn("TextProcessor", out)

    def test_methods_output(self):
        n, out = self._run("methods")
        self.assertGreater(n, 0)
        self.assertIn("createProcessor", out)

    def test_calls_output(self):
        n, out = self._run("calls", "process")
        self.assertGreater(n, 0)

    def test_implements_output(self):
        n, out = self._run("implements", "BaseProcessor")
        self.assertGreater(n, 0)
        self.assertIn("TextProcessor", out)

    def test_includes_output(self):
        n, out = self._run("includes")
        self.assertGreater(n, 0)
        self.assertIn("#include", out)

    def test_display_path_relative(self):
        n, out = self._run("classes")
        self.assertGreater(n, 0)
        self.assertIn("fixture.cpp", out)
        tmpdir_norm = self.tmpdir.replace("\\", "/")
        self.assertNotIn(tmpdir_norm, out)

    # ── consistency: process_cpp_file ↔ extract_cpp_metadata ─────────────────

    def test_class_names_consistent(self):
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("TextProcessor", meta["class_names"])
        n, out = self._run("classes")
        self.assertIn("TextProcessor", out)

    def test_call_sites_consistent(self):
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("process", meta["call_sites"])
        n, out = self._run("calls", "process")
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
