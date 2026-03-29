"""
Tests for C/C++ support: extract_cpp_metadata and query_cpp functions.

No server needed — all tests run against sample/root1/query_fixture.cpp.

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
HAL_FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "cpp", "hal_fixture.h")
CORNER_CASES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "cpp", "corner_cases.h")


def _setup_parser(path=None):
    lang = Language(tscpp.language())
    parser = Parser(lang)
    src = open(path or FIXTURE_PATH, "rb").read()
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
        matches = process_cpp_file(path=self.path, mode=mode, mode_arg=mode_arg)
        path_norm = self.path.replace("\\", "/")
        root_norm = self.tmpdir.replace("\\", "/").rstrip("/")
        disp = (path_norm[len(root_norm) + 1:]
                if path_norm.lower().startswith(root_norm.lower() + "/")
                else path_norm)
        out = "\n".join(f"{disp}:{m['line']}: {m['text']}" for m in (matches or []))
        return len(matches or []), out

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


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestHALFixture(unittest.TestCase):
    """Tests against hal_fixture.h — covers the four previously-buggy scenarios."""

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _setup_parser(HAL_FIXTURE_PATH)

    def _fx(self):
        return self.src, self.tree, self.lines

    # ── Bug 1: qualified base class — only base name, not namespace ───────────

    def test_implements_qualified_base(self):
        """ChibiOSAnalogIn : public HAL::AnalogIn → base is 'AnalogIn'."""
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "AnalogIn")
        self.assertTrue(has(r, "ChibiOSAnalogIn"), f"results={r}")

    def test_implements_no_namespace_as_base(self):
        """'HAL' must NOT appear as a base class name."""
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "HAL")
        self.assertEqual(len(r), 0, f"HAL spuriously matched: {r}")

    def test_template_base_name_not_arg(self):
        """LinuxScheduler : Scheduler<TimerTask> → base is 'Scheduler', not 'TimerTask'."""
        from src.query.cpp import cpp_q_implements
        r_sched = cpp_q_implements(*self._fx(), "Scheduler")
        self.assertTrue(has(r_sched, "LinuxScheduler"), f"results={r_sched}")
        r_task = cpp_q_implements(*self._fx(), "TimerTask")
        self.assertEqual(len(r_task), 0, f"TimerTask spuriously matched: {r_task}")

    def test_multiple_qualified_bases(self):
        """FullHALImpl : HAL::AnalogIn, HAL::AnalogSource → both bases found."""
        from src.query.cpp import cpp_q_implements
        r_ain  = cpp_q_implements(*self._fx(), "AnalogIn")
        r_asrc = cpp_q_implements(*self._fx(), "AnalogSource")
        self.assertTrue(has(r_ain,  "FullHALImpl"), f"AnalogIn results={r_ain}")
        self.assertTrue(has(r_asrc, "FullHALImpl"), f"AnalogSource results={r_asrc}")

    # ── Bug 1 (metadata): extract_cpp_metadata base_types ─────────────────────

    def test_metadata_qualified_base_types(self):
        """extract_cpp_metadata must list 'AnalogIn', not 'HAL', as base type."""
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(HAL_FIXTURE_PATH, "rb").read())
        self.assertIn("AnalogIn",   meta["base_types"])
        self.assertNotIn("HAL",     meta["base_types"])
        self.assertIn("Scheduler",  meta["base_types"])
        self.assertNotIn("TimerTask", meta["base_types"])

    # ── Bug 2: qualified class declarations ────────────────────────────────────

    def test_declarations_finds_namespace_class(self):
        """cpp_q_declarations finds HAL::AnalogIn by short name 'AnalogIn'."""
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "AnalogIn")
        self.assertGreater(len(r), 0, "AnalogIn declaration not found")

    def test_declarations_finds_nested_class(self):
        """cpp_q_declarations finds ChibiOSAnalogIn (top-level, qualified parent)."""
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "ChibiOSAnalogIn")
        self.assertGreater(len(r), 0)

    # ── Bug 3: qualified call sites ────────────────────────────────────────────

    def test_calls_qualified_function(self):
        """AP::panic() calls are found by bare name 'panic'."""
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "panic")
        self.assertGreater(len(r), 0, f"panic calls not found: {r}")

    def test_calls_qualified_multiple(self):
        """AP::panic() appears at least twice (two call sites)."""
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "panic")
        self.assertGreaterEqual(len(r), 2)

    def test_metadata_qualified_call_sites(self):
        """extract_cpp_metadata includes 'panic' from AP::panic() call sites."""
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(HAL_FIXTURE_PATH, "rb").read())
        self.assertIn("panic", meta["call_sites"])
        self.assertIn("hal_channel", meta["call_sites"])

    # ── Bug 4: pure-virtual / member function declarations ────────────────────

    def test_methods_finds_pure_virtual(self):
        """cpp_q_methods finds pure-virtual 'read' declared inside AnalogSource."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "read"), f"'read' not in methods: {r}")

    def test_methods_finds_pure_virtual_with_args(self):
        """cpp_q_methods finds pure-virtual 'set_pin' (has parameter)."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "set_pin"))

    def test_methods_pointer_return_member(self):
        """cpp_q_methods finds pointer-return pure-virtual 'get_source' in SensorManager."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "get_source"), f"'get_source' not in methods: {r}")

    def test_declarations_finds_pure_virtual_method(self):
        """cpp_q_declarations finds pure-virtual 'init' in AnalogIn."""
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "init")
        self.assertGreater(len(r), 0)

    def test_metadata_member_sigs_pure_virtual(self):
        """extract_cpp_metadata indexes signatures of pure-virtual methods."""
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(HAL_FIXTURE_PATH, "rb").read())
        self.assertTrue(any("read" in s for s in meta["member_sigs"]),
                        f"member_sigs={meta['member_sigs']}")
        self.assertTrue(any("init" in s for s in meta["member_sigs"]))

    def test_metadata_method_names_pure_virtual(self):
        """extract_cpp_metadata indexes names of pure-virtual member functions."""
        from indexserver.indexer import extract_cpp_metadata
        meta = extract_cpp_metadata(open(HAL_FIXTURE_PATH, "rb").read())
        self.assertIn("init",     meta["method_names"])
        self.assertIn("read",     meta["method_names"])
        self.assertIn("set_pin",  meta["method_names"])


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestCornerCases(unittest.TestCase):
    """Tests against corner_cases.h — pins the three bugs fixed in this session:
      1. Operator overloads (operator_name node) found by methods/declarations.
      2. Destructor names include the '~' prefix.
      3. Trailing-return-type prototypes (declaration node) found by methods/declarations.
    """

    @classmethod
    def setUpClass(cls):
        cls.src, cls.tree, cls.lines = _setup_parser(CORNER_CASES_PATH)

    def _fx(self):
        return self.src, self.tree, self.lines

    # ── Bug 1 fix: operator overloads appear in methods ───────────────────────

    def test_methods_finds_operator_plus(self):
        """Vec2::operator+ must appear in methods output."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "operator+"), f"results={r}")

    def test_methods_finds_operator_plus_assign(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "operator+="), f"results={r}")

    def test_methods_finds_operator_equal_equal(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "operator=="), f"results={r}")

    def test_methods_finds_operator_subscript(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "operator[]"), f"results={r}")

    def test_methods_finds_deleted_copy_assign(self):
        """NonCopyable::operator=(const NonCopyable&) = delete must appear."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        # At least one operator= should be found in NonCopyable
        matches = [t for _, t in r if "operator=" in t and "NonCopyable" in t]
        self.assertTrue(len(matches) > 0, f"results={r}")

    # ── Bug 1 fix: operator overloads found by declarations ───────────────────

    def test_declarations_finds_operator_plus(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "operator+")
        self.assertGreater(len(r), 0, f"operator+ not found in declarations")

    def test_declarations_finds_operator_assign(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "operator=")
        self.assertGreater(len(r), 0, f"operator= not found in declarations")

    # ── Bug 2 fix: destructor names include '~' ───────────────────────────────

    def test_methods_destructor_has_tilde(self):
        """~NonCopyable() must show as '~NonCopyable', not 'NonCopyable'."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        destructors = [t for _, t in r if "~" in t]
        self.assertTrue(len(destructors) > 0, f"No destructor with '~' found: {r}")
        self.assertTrue(any("~NonCopyable" in t for t in destructors),
                        f"~NonCopyable not found; destructor entries: {destructors}")

    def test_methods_iwidget_destructor_has_tilde(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "~IWidget"), f"~IWidget not found: {r}")

    def test_declarations_destructor_has_tilde(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "~NonCopyable")
        self.assertGreater(len(r), 0, "~NonCopyable not found via declarations")

    # ── Bug 3 fix: trailing-return-type prototypes found ─────────────────────

    def test_methods_trailing_return_free_function(self):
        """auto compute_sum(int a, int b) -> int must appear in methods."""
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "compute_sum"), f"compute_sum not in methods: {r}")

    def test_methods_trailing_return_second_prototype(self):
        from src.query.cpp import cpp_q_methods
        r = cpp_q_methods(*self._fx())
        self.assertTrue(has(r, "make_label"), f"make_label not in methods: {r}")

    def test_declarations_trailing_return_type(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "compute_sum")
        self.assertGreater(len(r), 0, "compute_sum not found via declarations")

    def test_declarations_trailing_return_second(self):
        from src.query.cpp import cpp_q_declarations
        r = cpp_q_declarations(*self._fx(), "make_label")
        self.assertGreater(len(r), 0, "make_label not found via declarations")

    # ── Other corner cases: classes ───────────────────────────────────────────

    def test_classes_finds_vec2(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(has(r, "Vec2"))

    def test_classes_finds_nested_label(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(has(r, "Label"))

    def test_classes_finds_template_class(self):
        from src.query.cpp import cpp_q_classes
        r = cpp_q_classes(*self._fx())
        self.assertTrue(has(r, "RingBuffer"))

    # ── Other corner cases: implements ────────────────────────────────────────

    def test_implements_multiple_inheritance(self):
        """Sensor : ISerializable, ILoggable — both bases should match."""
        from src.query.cpp import cpp_q_implements
        r_ser = cpp_q_implements(*self._fx(), "ISerializable")
        r_log = cpp_q_implements(*self._fx(), "ILoggable")
        self.assertTrue(has(r_ser, "Sensor"), f"ISerializable results={r_ser}")
        self.assertTrue(has(r_log, "Sensor"), f"ILoggable results={r_log}")

    def test_implements_virtual_base(self):
        """Plane : virtual VehicleBase — VehicleBase should match."""
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "VehicleBase")
        self.assertTrue(has(r, "Plane"), f"VehicleBase results={r}")

    def test_implements_diamond(self):
        """VTOL : Plane, Copter — both bases should match."""
        from src.query.cpp import cpp_q_implements
        r_plane = cpp_q_implements(*self._fx(), "Plane")
        r_copter = cpp_q_implements(*self._fx(), "Copter")
        self.assertTrue(has(r_plane, "VTOL"), f"Plane results={r_plane}")
        self.assertTrue(has(r_copter, "VTOL"), f"Copter results={r_copter}")

    def test_implements_nested_class_inherits_iwidget(self):
        """Panel::Label : IWidget — Label should match IWidget."""
        from src.query.cpp import cpp_q_implements
        r = cpp_q_implements(*self._fx(), "IWidget")
        self.assertTrue(has(r, "Label"), f"IWidget results={r}")
        self.assertTrue(has(r, "Panel"), f"Panel not in IWidget results={r}")

    # ── Other corner cases: calls ─────────────────────────────────────────────

    def test_calls_inside_lambda(self):
        """helper() called inside a lambda in fire_all must be found."""
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "helper")
        self.assertGreater(len(r), 0, f"helper call not found: {r}")

    def test_calls_register_cb(self):
        from src.query.cpp import cpp_q_calls
        r = cpp_q_calls(*self._fx(), "register_cb")
        self.assertGreater(len(r), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
