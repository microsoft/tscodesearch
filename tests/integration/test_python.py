"""
Integration tests for Python field discrimination.

The fixtures position one identifier (``IFoo`` for type usage; ``process`` for
method/call) in exactly one structural role per file. Each search-by-field
test asserts the file with that role comes back and the files that mention
the identifier in other roles do not.

TestPySemanticFieldDiscrim -- end-to-end against a real Tantivy index.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _assert_server_ok, _search, _delete_collection, _make_git_repo,
)
from query.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


# -- Fixtures: each file places identifiers in exactly one role ---------------

_BASE_IMPLEMENTOR_PY = """\
import os

class IFoo:
    def process(self, data: str) -> None:
        pass

class Implementor(IFoo):
    def process(self, data: str) -> None:
        print(data)
"""

_CALLER_PY = """\
from app.impl import Implementor

class Caller:
    def __init__(self, target: Implementor) -> None:
        self._target = target

    def run(self) -> None:
        self._target.process("hello")
"""

_DECORATED_PY = """\
def dataclass(cls):
    return cls

@dataclass
class Decorated:
    name: str = ""
"""

_IMPORTER_PY = """\
import json
from typing import Optional

class JsonHelper:
    def encode(self, obj) -> Optional[str]:
        return json.dumps(obj)
"""

_STRING_MENTION_PY = """\
class StringMention:
    description = "implements IFoo for cleanup"
    note = "calls process at runtime"
"""

_UNRELATED_PY = """\
class Unrelated:
    greeting = "hello"
"""


class TestPySemanticFieldDiscrim(unittest.TestCase):
    """Search by each field for a known identifier and assert which Python
    file comes back. No re-parsing on the test side."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_py_discrim_{stamp}"
        cls.tmpdir = _make_git_repo({
            "app/impl.py":           _BASE_IMPLEMENTOR_PY,
            "app/caller.py":         _CALLER_PY,
            "app/decorated.py":      _DECORATED_PY,
            "app/importer.py":       _IMPORTER_PY,
            "app/string_mention.py": _STRING_MENTION_PY,
            "app/unrelated.py":      _UNRELATED_PY,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _files(self, q: str, query_by: str) -> set:
        hits = _search(self.coll, q, query_by=query_by, per_page=20)
        return {h["filename"] for h in hits}

    # -- class_names: only declared classes ------------------------------------

    def test_class_names_finds_declaring_file(self):
        # ``Implementor`` is declared in impl.py and used as a param type in
        # caller.py. class_names should match only the declaring file.
        self.assertEqual(self._files("Implementor", "class_names"),
                         {"impl.py"})

    # -- method_names: only declared methods -----------------------------------

    def test_method_names_finds_only_declaring_files(self):
        # Both ``IFoo`` and ``Implementor`` (in impl.py) declare ``process``.
        # caller.py CALLS ``process`` but doesn't define it.
        self.assertEqual(self._files("process", "method_names"),
                         {"impl.py"})

    # -- base_types: subclass-of relationship ----------------------------------

    def test_base_types_finds_only_subclass(self):
        # ``Implementor(IFoo)`` is the only subclass relation referencing IFoo.
        self.assertEqual(self._files("IFoo", "base_types"),
                         {"impl.py"})

    # -- call_sites: only the call expression ----------------------------------

    def test_call_sites_finds_only_caller(self):
        # caller.py calls .process(); impl.py declares it.
        self.assertEqual(self._files("process", "call_sites"),
                         {"caller.py"})

    # -- attr_names: only decorator usages -------------------------------------

    def test_attr_names_finds_only_decorated_file(self):
        self.assertEqual(self._files("dataclass", "attr_names"),
                         {"decorated.py"})

    # -- imports: only the importing file --------------------------------------

    def test_imports_finds_only_importer(self):
        self.assertEqual(self._files("json", "imports"),
                         {"importer.py"})

    def test_imports_excludes_files_without_that_import(self):
        # impl.py imports os, not json. caller.py imports app.impl, not json.
        files = self._files("os", "imports")
        self.assertEqual(files, {"impl.py"},
            f"only impl.py imports os, got {files}")

    # -- string-literal mentions don't leak into structured fields -------------

    def test_string_mentions_excluded_from_structured_fields(self):
        for field in ("class_names", "method_names", "base_types",
                      "call_sites", "attr_names", "imports", "tokens"):
            self.assertNotIn(
                "string_mention.py", self._files("IFoo", field),
                f"string_mention.py leaked into {field} (string literal)",
            )
            self.assertNotIn(
                "string_mention.py", self._files("process", field),
                f"string_mention.py leaked into {field} (string literal)",
            )

    # -- unrelated files never come back ---------------------------------------

    def test_unrelated_file_excluded_from_every_search(self):
        for ident, field in (
            ("IFoo",        "base_types"),
            ("process",     "method_names"),
            ("Implementor", "class_names"),
            ("dataclass",   "attr_names"),
            ("json",        "imports"),
        ):
            self.assertNotIn(
                "unrelated.py", self._files(ident, field),
                f"unrelated.py should not appear for {ident}/{field}",
            )


if __name__ == "__main__":
    unittest.main()
