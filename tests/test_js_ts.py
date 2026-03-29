"""
Tests for JavaScript and TypeScript support: extract metadata and query functions.

No server needed — all tests run against sample/root1/query_fixture.js and
sample/root1/query_fixture.ts.

Run from WSL:
    ~/.local/indexserver-venv/bin/pytest tests/test_js_ts.py -v
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
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser as _Parser
    _JS_AVAILABLE = True
except ImportError:
    _JS_AVAILABLE = False

try:
    import tree_sitter_typescript as tsts
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False

_SKIP_JS = not _JS_AVAILABLE
_SKIP_TS = not _TS_AVAILABLE
_SKIP_MSG_JS = "tree-sitter-javascript not installed"
_SKIP_MSG_TS = "tree-sitter-typescript not installed"

JS_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "query_fixture.js")
TS_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "query_fixture.ts")


def _parse(fixture_path, is_ts=False, is_tsx=False):
    if is_tsx:
        lang = Language(tsts.language_tsx())
    elif is_ts:
        lang = Language(tsts.language_typescript())
    else:
        lang = Language(tsjs.language())
    parser = _Parser(lang)
    src = open(fixture_path, "rb").read()
    tree = parser.parse(src)
    lines = src.decode("utf-8", errors="replace").splitlines()
    return src, tree, lines


def has(results, sub):
    return any(sub in t for _, t in results)


# ── JavaScript tests ──────────────────────────────────────────────────────────

@unittest.skipIf(_SKIP_JS, _SKIP_MSG_JS)
class TestExtractJsMetadata(unittest.TestCase):
    """Unit tests for extract_js_metadata — no server needed."""

    @classmethod
    def setUpClass(cls):
        from indexserver.indexer import extract_js_metadata
        cls._meta = extract_js_metadata(open(JS_FIXTURE, "rb").read())

    def test_class_names_indexed(self):
        self.assertIn("TextProcessor", self._meta["class_names"])

    def test_base_types_indexed(self):
        self.assertIn("Processor", self._meta["base_types"])

    def test_method_names_indexed(self):
        self.assertIn("process", self._meta["method_names"])

    def test_call_sites_indexed(self):
        self.assertIn("process", self._meta["call_sites"])

    def test_imports_in_usings(self):
        self.assertIn("events", self._meta["usings"])

    def test_member_sigs_indexed(self):
        sigs = self._meta["member_sigs"]
        self.assertTrue(any("createProcessor" in s for s in sigs), f"sigs={sigs}")


@unittest.skipIf(_SKIP_JS, _SKIP_MSG_JS)
class TestQueryJs(unittest.TestCase):
    """Unit tests for JS query functions."""

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _parse(JS_FIXTURE)

    def _fx(self):
        return self.src, self.tree, self.lines

    def test_classes_finds_class(self):
        from src.query.js import js_q_classes
        r = js_q_classes(*self._fx())
        self.assertTrue(has(r, "TextProcessor"))

    def test_classes_shows_extends(self):
        from src.query.js import js_q_classes
        r = js_q_classes(*self._fx())
        match = next((t for _, t in r if "TextProcessor" in t), None)
        self.assertIsNotNone(match)
        self.assertIn("Processor", match)

    def test_methods_finds_function(self):
        from src.query.js import js_q_methods
        r = js_q_methods(*self._fx())
        self.assertTrue(has(r, "createProcessor"))

    def test_methods_finds_method(self):
        from src.query.js import js_q_methods
        r = js_q_methods(*self._fx())
        self.assertTrue(has(r, "process"))

    def test_calls_finds_function_call(self):
        from src.query.js import js_q_calls
        r = js_q_calls(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)

    def test_calls_finds_method_call(self):
        from src.query.js import js_q_calls
        r = js_q_calls(*self._fx(), "process")
        self.assertGreater(len(r), 0)

    def test_calls_absent_no_match(self):
        from src.query.js import js_q_calls
        r = js_q_calls(*self._fx(), "nonexistentXYZ")
        self.assertEqual(len(r), 0)

    def test_implements_finds_extending_class(self):
        from src.query.js import js_q_implements
        r = js_q_implements(*self._fx(), "Processor")
        self.assertGreater(len(r), 0)
        self.assertTrue(has(r, "TextProcessor"))

    def test_implements_nonexistent_no_match(self):
        from src.query.js import js_q_implements
        r = js_q_implements(*self._fx(), "INonExistent999")
        self.assertEqual(len(r), 0)

    def test_declarations_finds_class(self):
        from src.query.js import js_q_declarations
        r = js_q_declarations(*self._fx(), "TextProcessor")
        self.assertGreater(len(r), 0)

    def test_declarations_finds_function(self):
        from src.query.js import js_q_declarations
        r = js_q_declarations(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)

    def test_all_refs_finds_identifier(self):
        from src.query.js import js_q_all_refs
        r = js_q_all_refs(*self._fx(), "TextProcessor")
        self.assertGreater(len(r), 0)

    def test_imports_found(self):
        from src.query.js import js_q_imports
        r = js_q_imports(*self._fx())
        self.assertGreater(len(r), 0)
        self.assertTrue(any("import" in t for _, t in r))

    def test_params_found(self):
        from src.query.js import js_q_params
        r = js_q_params(*self._fx(), "createProcessor")
        self.assertGreater(len(r), 0)


# ── TypeScript tests ──────────────────────────────────────────────────────────

@unittest.skipIf(_SKIP_TS, _SKIP_MSG_TS)
class TestExtractTsMetadata(unittest.TestCase):
    """Unit tests for extract_ts_metadata — no server needed."""

    @classmethod
    def setUpClass(cls):
        from indexserver.indexer import extract_ts_metadata
        cls._meta = extract_ts_metadata(open(TS_FIXTURE, "rb").read())

    def test_class_names_indexed(self):
        self.assertIn("TextProcessor", self._meta["class_names"])

    def test_interface_in_class_names(self):
        self.assertIn("IProcessor", self._meta["class_names"])

    def test_base_types_from_extends(self):
        self.assertIn("BaseProcessor", self._meta["base_types"])

    def test_base_types_from_implements(self):
        self.assertIn("IProcessor", self._meta["base_types"])

    def test_method_names_indexed(self):
        self.assertIn("process", self._meta["method_names"])

    def test_call_sites_indexed(self):
        self.assertIn("process", self._meta["call_sites"])

    def test_decorator_in_attr_names(self):
        self.assertIn("serializable", self._meta["attr_names"])

    def test_imports_in_usings(self):
        self.assertIn("events", self._meta["usings"])


@unittest.skipIf(_SKIP_TS, _SKIP_MSG_TS)
class TestQueryTs(unittest.TestCase):
    """Unit tests for TS query functions (uses TypeScript grammar)."""

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _parse(TS_FIXTURE, is_ts=True)

    def _fx(self):
        return self.src, self.tree, self.lines

    def test_classes_finds_class(self):
        from src.query.js import js_q_classes
        r = js_q_classes(*self._fx())
        self.assertTrue(has(r, "TextProcessor"))

    def test_classes_finds_interface(self):
        from src.query.js import js_q_classes
        r = js_q_classes(*self._fx())
        self.assertTrue(has(r, "IProcessor"))

    def test_classes_finds_enum(self):
        from src.query.js import js_q_classes
        r = js_q_classes(*self._fx())
        self.assertTrue(has(r, "ProcessingMode"))

    def test_methods_finds_typed_method(self):
        from src.query.js import js_q_methods
        r = js_q_methods(*self._fx())
        self.assertTrue(has(r, "process"))

    def test_calls_found(self):
        from src.query.js import js_q_calls
        r = js_q_calls(*self._fx(), "process")
        self.assertGreater(len(r), 0)

    def test_implements_finds_extending_class(self):
        from src.query.js import js_q_implements
        r = js_q_implements(*self._fx(), "BaseProcessor")
        self.assertGreater(len(r), 0)
        self.assertTrue(has(r, "TextProcessor"))

    def test_attrs_finds_decorator(self):
        from src.query.js import js_q_attrs
        r = js_q_attrs(*self._fx())
        self.assertGreater(len(r), 0)
        self.assertTrue(any("serializable" in t for _, t in r))

    def test_attrs_filtered_by_name(self):
        from src.query.js import js_q_attrs
        r = js_q_attrs(*self._fx(), "serializable")
        self.assertGreater(len(r), 0)

    def test_attrs_filter_no_match(self):
        from src.query.js import js_q_attrs
        r = js_q_attrs(*self._fx(), "nonexistent_decorator_xyz")
        self.assertEqual(len(r), 0)


@unittest.skipIf(_SKIP_JS, _SKIP_MSG_JS)
class TestProcessJsFile(unittest.TestCase):
    """Tests for process_js_file — uses actual file I/O."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_js_test_")
        cls.js_path = os.path.join(cls.tmpdir, "fixture.js")
        shutil.copy(JS_FIXTURE, cls.js_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run(self, path, mode, mode_arg=None):
        from src.query.dispatch import process_js_file
        matches = process_js_file(path=path, mode=mode, mode_arg=mode_arg)
        path_norm = path.replace("\\", "/")
        root_norm = self.tmpdir.replace("\\", "/").rstrip("/")
        disp = (path_norm[len(root_norm) + 1:]
                if path_norm.lower().startswith(root_norm.lower() + "/")
                else path_norm)
        out = "\n".join(f"{disp}:{m['line']}: {m['text']}" for m in (matches or []))
        return len(matches or []), out

    def test_classes_output(self):
        n, out = self._run(self.js_path, "classes")
        self.assertGreater(n, 0)
        self.assertIn("TextProcessor", out)

    def test_methods_output(self):
        n, out = self._run(self.js_path, "methods")
        self.assertGreater(n, 0)
        self.assertIn("createProcessor", out)

    def test_calls_output(self):
        n, out = self._run(self.js_path, "calls", "process")
        self.assertGreater(n, 0)

    def test_implements_output(self):
        n, out = self._run(self.js_path, "implements", "Processor")
        self.assertGreater(n, 0)
        self.assertIn("TextProcessor", out)

    def test_imports_output(self):
        n, out = self._run(self.js_path, "imports")
        self.assertGreater(n, 0)
        self.assertIn("import", out)

    def test_display_path_relative(self):
        n, out = self._run(self.js_path, "classes")
        self.assertGreater(n, 0)
        self.assertIn("fixture.js", out)
        tmpdir_norm = self.tmpdir.replace("\\", "/")
        self.assertNotIn(tmpdir_norm, out)

    # ── consistency: process_js_file ↔ extract_js_metadata ───────────────────

    def test_class_names_consistent(self):
        from indexserver.indexer import extract_js_metadata
        meta = extract_js_metadata(open(JS_FIXTURE, "rb").read())
        self.assertIn("TextProcessor", meta["class_names"])
        n, out = self._run(self.js_path, "classes")
        self.assertIn("TextProcessor", out)

    def test_call_sites_consistent(self):
        from indexserver.indexer import extract_js_metadata
        meta = extract_js_metadata(open(JS_FIXTURE, "rb").read())
        self.assertIn("process", meta["call_sites"])
        n, out = self._run(self.js_path, "calls", "process")
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
