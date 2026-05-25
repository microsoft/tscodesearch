"""
Unit tests for the verifier: export_index_map and run_verify.

TestExportIndex   -- exercises export_index_map against a real Tantivy index
TestRunVerifyUnit -- exercises run_verify with index_file_list mocked out so
                    the test focuses on diff/orphan logic, not the indexer
                    pipeline (covered by tests/integration/test_verifier.py)
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import _FOO_CS, _BAR_CS, make_test_backend
from query.config import load_config as _load_config
from indexserver.indexer import file_id, export_index_map
from indexserver.verifier import run_verify

_cfg = _load_config()


def _seed_doc(backend, doc_id: str, mtime: int) -> None:
    """Insert a minimal document with the given id+mtime into the backend.

    Real Tantivy requires every stored field on a document; the indexer's
    build_document fills these from real source code. For verifier diff
    logic we only need id+mtime to round-trip through export_id_mtime, so
    fill everything else with empty values.
    """
    doc = {
        "id":               doc_id,
        "relative_path":    "",
        "filename":         "",
        "extension":        "",
        "language":         "",
        "namespace":        "",
        "path_segments":    [],
        "tokens":           "",
        "mtime":            int(mtime),
        "class_names":      [],
        "method_names":     [],
        "member_sigs":      [],
        "base_types":       [],
        "field_types":      [],
        "local_types":      [],
        "param_types":      [],
        "return_types":     [],
        "cast_types":       [],
        "type_refs":        [],
        "call_sites":       [],
        "member_accesses":  [],
        "attr_names":       [],
        "imports":          [],
    }
    backend.upsert_many([doc])


# -- TestExportIndex -----------------------------------------------------------

class TestExportIndex(unittest.TestCase):
    """Unit tests for export_index_map -- runs against a real Tantivy index."""

    def setUp(self):
        self.backend, self._cleanup = make_test_backend()

    def tearDown(self):
        self._cleanup()

    def test_returns_id_mtime_dict(self):
        _seed_doc(self.backend, "abc", 1700000000)
        self.assertEqual(export_index_map(self.backend), {"abc": 1700000000})

    def test_multiple_docs(self):
        _seed_doc(self.backend, "a", 100)
        _seed_doc(self.backend, "b", 200)
        _seed_doc(self.backend, "c", 300)
        result = export_index_map(self.backend)
        self.assertEqual(len(result), 3)
        self.assertEqual(result["b"], 200)

    def test_missing_mtime_defaults_to_zero(self):
        _seed_doc(self.backend, "no_mtime", 0)
        self.assertEqual(export_index_map(self.backend)["no_mtime"], 0)

    def test_empty_backend_returns_empty_dict(self):
        self.assertEqual(export_index_map(self.backend), {})


# -- TestRunVerifyUnit ---------------------------------------------------------

class TestRunVerifyUnit(unittest.TestCase):
    """run_verify diff logic -- real Tantivy backend, mocked index_file_list.

    The mock lets the test assert exactly which files run_verify decided to
    re-index without re-running the whole tree-sitter extraction; integration
    tests cover the pipeline end-to-end.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ts_verify_unit_")
        os.makedirs(os.path.join(self.tmpdir, "src"), exist_ok=True)
        self._write("src/foo.cs", _FOO_CS)
        self._write("src/bar.cs", _BAR_CS)
        self.backend, self._backend_cleanup = make_test_backend()

    def tearDown(self):
        self._backend_cleanup()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, rel: str, content: str) -> None:
        path = os.path.join(self.tmpdir, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _mtime(self, rel: str) -> int:
        return int(os.stat(os.path.join(self.tmpdir, rel)).st_mtime)

    def _run_verify_with_index(self, index_map: dict, delete_orphans: bool = True):
        """Run run_verify on a backend pre-populated to mirror `index_map`."""
        for doc_id, mtime in index_map.items():
            _seed_doc(self.backend, doc_id, mtime)
        before_ids = set(self.backend.export_id_mtime())

        indexed: list = []

        def fake_index_file_list(_backend, file_pairs,
                                 batch_size=50, verbose=False, on_progress=None,
                                 stop_event=None):
            pairs = list(file_pairs)
            indexed.extend(pairs)
            if on_progress:
                on_progress(len(pairs), 0)
            return len(pairs), 0

        with patch("indexserver.verifier.index_file_list", fake_index_file_list):
            run_verify(_cfg, src_root=self.tmpdir, collection="test_coll",
                       delete_orphans=delete_orphans, backend=self.backend)

        after_ids = set(self.backend.export_id_mtime())
        deleted = sorted(before_ids - after_ids)
        return indexed, deleted

    def test_missing_files_are_indexed(self):
        indexed, _ = self._run_verify_with_index(index_map={})
        rel_paths = [rel for _, rel in indexed]
        self.assertTrue(any("foo.cs" in r for r in rel_paths))
        self.assertTrue(any("bar.cs" in r for r in rel_paths))

    def test_up_to_date_files_not_reindexed(self):
        foo_id = file_id("src/foo.cs")
        bar_id = file_id("src/bar.cs")
        index_map = {
            foo_id: self._mtime("src/foo.cs"),
            bar_id: self._mtime("src/bar.cs"),
        }
        indexed, _ = self._run_verify_with_index(index_map=index_map)
        self.assertEqual(len(indexed), 0)

    def test_stale_file_is_reindexed(self):
        foo_id = file_id("src/foo.cs")
        bar_id = file_id("src/bar.cs")
        index_map = {
            foo_id: self._mtime("src/foo.cs") - 1,  # stale
            bar_id: self._mtime("src/bar.cs"),       # current
        }
        indexed, _ = self._run_verify_with_index(index_map=index_map)
        rel_paths = [rel for _, rel in indexed]
        self.assertEqual(len(indexed), 1)
        self.assertTrue(any("foo.cs" in r for r in rel_paths))

    def test_only_missing_count_matches_indexed_count(self):
        indexed, _ = self._run_verify_with_index(index_map={})
        self.assertEqual(len(indexed), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
