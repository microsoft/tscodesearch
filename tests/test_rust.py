"""
Tests for Rust support: extract_rust_metadata and query_rust functions.

No server needed — all tests run against tests/query_fixture.rs.

Run from WSL:
    ~/.local/indexserver-venv/bin/pytest tests/test_rust.py -v
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
    import tree_sitter_rust as tsrust
    from tree_sitter import Language, Parser
    _RUST_AVAILABLE = True
except ImportError:
    _RUST_AVAILABLE = False

_SKIP = not _RUST_AVAILABLE
_SKIP_MSG = "tree-sitter-rust not installed"

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "query_fixture.rs")


def _setup_parser():
    lang = Language(tsrust.language())
    parser = Parser(lang)
    src = open(FIXTURE_PATH, "rb").read()
    tree = parser.parse(src)
    lines = src.decode("utf-8", errors="replace").splitlines()
    return src, tree, lines


def texts(results):
    return [t for _, t in results]


def has(results, sub):
    return any(sub in t for _, t in results)


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestExtractRustMetadata(unittest.TestCase):
    """Unit tests for extract_rust_metadata — no server needed."""

    @classmethod
    def setUpClass(cls):
        from indexserver.indexer import extract_rust_metadata
        cls._meta = extract_rust_metadata(open(FIXTURE_PATH, "rb").read())

    def test_struct_names_indexed(self):
        self.assertIn("ProcessResult", self._meta["class_names"])

    def test_trait_names_indexed(self):
        self.assertIn("Processor", self._meta["class_names"])

    def test_function_names_indexed(self):
        self.assertIn("create_processor", self._meta["method_names"])

    def test_impl_method_names_indexed(self):
        self.assertIn("process", self._meta["method_names"])

    def test_base_types_from_impl_trait(self):
        self.assertIn("Processor", self._meta["base_types"])

    def test_call_sites_indexed(self):
        self.assertIn("process", self._meta["call_sites"])

    def test_use_imports_in_usings(self):
        self.assertIn("std", self._meta["usings"])

    def test_member_sigs_contain_fn(self):
        sigs = self._meta["member_sigs"]
        self.assertTrue(any("create_processor" in s for s in sigs), f"sigs={sigs}")


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestQueryRust(unittest.TestCase):
    """Unit tests for Rust query functions."""

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _setup_parser()

    def _fx(self):
        return self.src, self.tree, self.lines

    # ── classes ──────────────────────────────────────────────────────────────

    def test_classes_finds_struct(self):
        from src.query.rust import rust_q_classes
        r = rust_q_classes(*self._fx())
        self.assertTrue(has(r, "ProcessResult"))

    def test_classes_finds_trait(self):
        from src.query.rust import rust_q_classes
        r = rust_q_classes(*self._fx())
        self.assertTrue(has(r, "Processor"))

    def test_classes_finds_enum(self):
        from src.query.rust import rust_q_classes
        r = rust_q_classes(*self._fx())
        self.assertTrue(has(r, "ProcessingMode"))

    def test_classes_kind_tagged(self):
        from src.query.rust import rust_q_classes
        r = rust_q_classes(*self._fx())
        structs = [t for _, t in r if "[struct]" in t]
        self.assertTrue(any("ProcessResult" in t for t in structs))

    # ── methods ──────────────────────────────────────────────────────────────

    def test_methods_finds_function(self):
        from src.query.rust import rust_q_methods
        r = rust_q_methods(*self._fx())
        self.assertTrue(has(r, "create_processor"))

    def test_methods_finds_impl_method(self):
        from src.query.rust import rust_q_methods
        r = rust_q_methods(*self._fx())
        self.assertTrue(has(r, "process"))

    def test_methods_shows_impl_context(self):
        from src.query.rust import rust_q_methods
        r = rust_q_methods(*self._fx())
        self.assertTrue(any("[in " in t for _, t in r))

    # ── calls ─────────────────────────────────────────────────────────────────

    def test_calls_finds_function_call(self):
        from src.query.rust import rust_q_calls
        r = rust_q_calls(*self._fx(), "create_processor")
        self.assertGreater(len(r), 0)

    def test_calls_finds_method_call(self):
        from src.query.rust import rust_q_calls
        r = rust_q_calls(*self._fx(), "process")
        self.assertGreater(len(r), 0)

    def test_calls_absent_no_match(self):
        from src.query.rust import rust_q_calls
        r = rust_q_calls(*self._fx(), "nonexistent_func_xyz")
        self.assertEqual(len(r), 0)

    def test_calls_skips_comment(self):
        from src.query.rust import rust_q_calls
        # "process" appears in a comment; should not double-count that line
        r = rust_q_calls(*self._fx(), "process")
        for ln, text in r:
            self.assertNotIn("COMMENT", text)

    # ── implements ──────────────────────────────────────────────────────────

    def test_implements_finds_processor_impl(self):
        from src.query.rust import rust_q_implements
        r = rust_q_implements(*self._fx(), "Processor")
        self.assertGreater(len(r), 0)
        self.assertTrue(has(r, "TextProcessor"))

    def test_implements_finds_logger_impl(self):
        from src.query.rust import rust_q_implements
        r = rust_q_implements(*self._fx(), "Logger")
        self.assertGreater(len(r), 0)

    def test_implements_nonexistent_no_match(self):
        from src.query.rust import rust_q_implements
        r = rust_q_implements(*self._fx(), "INonExistent999")
        self.assertEqual(len(r), 0)

    # ── declarations ────────────────────────────────────────────────────────

    def test_declarations_finds_struct(self):
        from src.query.rust import rust_q_declarations
        r = rust_q_declarations(*self._fx(), "ProcessResult")
        self.assertGreater(len(r), 0)
        self.assertTrue(has(r, "ProcessResult"))

    def test_declarations_finds_function(self):
        from src.query.rust import rust_q_declarations
        r = rust_q_declarations(*self._fx(), "create_processor")
        self.assertGreater(len(r), 0)

    def test_declarations_nonexistent_no_match(self):
        from src.query.rust import rust_q_declarations
        r = rust_q_declarations(*self._fx(), "ZZZNonExistentXXX")
        self.assertEqual(len(r), 0)

    # ── all_refs ────────────────────────────────────────────────────────────

    def test_all_refs_finds_type(self):
        from src.query.rust import rust_q_all_refs
        r = rust_q_all_refs(*self._fx(), "ProcessResult")
        self.assertGreater(len(r), 0)

    def test_all_refs_absent_no_match(self):
        from src.query.rust import rust_q_all_refs
        r = rust_q_all_refs(*self._fx(), "ZZZNonExistentXXX")
        self.assertEqual(len(r), 0)

    # ── imports ─────────────────────────────────────────────────────────────

    def test_imports_found(self):
        from src.query.rust import rust_q_imports
        r = rust_q_imports(*self._fx())
        self.assertGreater(len(r), 0)
        self.assertTrue(any("use" in t for _, t in r))

    def test_imports_contains_std(self):
        from src.query.rust import rust_q_imports
        r = rust_q_imports(*self._fx())
        self.assertTrue(any("std" in t for _, t in r))

    # ── params ──────────────────────────────────────────────────────────────

    def test_params_found(self):
        from src.query.rust import rust_q_params
        r = rust_q_params(*self._fx(), "create_processor")
        self.assertGreater(len(r), 0)

    def test_params_absent_no_match(self):
        from src.query.rust import rust_q_params
        r = rust_q_params(*self._fx(), "nonexistent_xyz")
        self.assertEqual(len(r), 0)


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestProcessRustFile(unittest.TestCase):
    """Tests for process_rust_file — uses actual file I/O."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_rust_test_")
        cls.path = os.path.join(cls.tmpdir, "fixture.rs")
        import shutil
        shutil.copy(FIXTURE_PATH, cls.path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, mode, mode_arg=None):
        from src.query.dispatch import process_rust_file
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            n = process_rust_file(
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
        self.assertIn("ProcessResult", out)

    def test_methods_output(self):
        n, out = self._run("methods")
        self.assertGreater(n, 0)
        self.assertIn("create_processor", out)

    def test_calls_output(self):
        n, out = self._run("calls", "process")
        self.assertGreater(n, 0)

    def test_implements_output(self):
        n, out = self._run("implements", "Processor")
        self.assertGreater(n, 0)
        self.assertIn("TextProcessor", out)

    def test_imports_output(self):
        n, out = self._run("imports")
        self.assertGreater(n, 0)
        self.assertIn("use", out)

    def test_display_path_is_relative(self):
        n, out = self._run("classes")
        self.assertGreater(n, 0)
        self.assertIn("fixture.rs", out)
        tmpdir_norm = self.tmpdir.replace("\\", "/")
        self.assertNotIn(tmpdir_norm, out)

    # ── consistency: process_rust_file ↔ extract_rust_metadata ───────────────

    def test_class_names_consistent(self):
        from indexserver.indexer import extract_rust_metadata
        meta = extract_rust_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("ProcessResult", meta["class_names"])
        n, out = self._run("classes")
        self.assertIn("ProcessResult", out)

    def test_method_names_consistent(self):
        from indexserver.indexer import extract_rust_metadata
        meta = extract_rust_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("create_processor", meta["method_names"])
        n, out = self._run("methods")
        self.assertIn("create_processor", out)

    def test_call_sites_consistent(self):
        from indexserver.indexer import extract_rust_metadata
        meta = extract_rust_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("process", meta["call_sites"])
        n, out = self._run("calls", "process")
        self.assertGreater(n, 0)

    def test_base_types_consistent(self):
        from indexserver.indexer import extract_rust_metadata
        meta = extract_rust_metadata(open(FIXTURE_PATH, "rb").read())
        self.assertIn("Processor", meta["base_types"])
        n, out = self._run("implements", "Processor")
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
