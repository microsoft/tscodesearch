"""
Unit tests for the verifier: export_index_map and run_verify.

TestExportIndex   — unit tests for export_index_map against a fake backend
TestRunVerifyUnit — unit tests for run_verify with mocked index_file_list

Integration tests (require a real Tantivy index) live in tests/integration/test_verifier.py.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import _FOO_CS, _BAR_CS, _FakeBackend
from indexserver.config import load_config as _load_config
from indexserver.indexer import file_id, export_index_map
from indexserver.verifier import run_verify

_cfg = _load_config()


# ── TestExportIndex ───────────────────────────────────────────────────────────

class TestExportIndex(unittest.TestCase):
    """Unit tests for export_index_map — runs against the fake backend."""

    def test_returns_id_mtime_dict(self):
        b = _FakeBackend()
        b.upsert_many([{"id": "abc", "mtime": 1700000000}])
        self.assertEqual(export_index_map(b), {"abc": 1700000000})

    def test_multiple_docs(self):
        b = _FakeBackend()
        b.upsert_many([
            {"id": "a", "mtime": 100},
            {"id": "b", "mtime": 200},
            {"id": "c", "mtime": 300},
        ])
        result = export_index_map(b)
        self.assertEqual(len(result), 3)
        self.assertEqual(result["b"], 200)

    def test_missing_mtime_defaults_to_zero(self):
        b = _FakeBackend()
        b.upsert_many([{"id": "no_mtime"}])
        self.assertEqual(export_index_map(b)["no_mtime"], 0)

    def test_empty_backend_returns_empty_dict(self):
        b = _FakeBackend()
        self.assertEqual(export_index_map(b), {})


# ── TestRunVerifyUnit ─────────────────────────────────────────────────────────

class TestRunVerifyUnit(unittest.TestCase):
    """Unit tests for run_verify logic — fake backend; mocked index_file_list."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ts_verify_unit_")
        os.makedirs(os.path.join(self.tmpdir, "src"), exist_ok=True)
        self._write("src/foo.cs", _FOO_CS)
        self._write("src/bar.cs", _BAR_CS)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, rel: str, content: str) -> None:
        path = os.path.join(self.tmpdir, rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _mtime(self, rel: str) -> int:
        return int(os.stat(os.path.join(self.tmpdir, rel)).st_mtime)

    def _run_verify_with_backend(self, index_map: dict, delete_orphans: bool = True):
        """Run run_verify with a fake backend pre-populated by `index_map`."""
        indexed: list = []
        backend = _FakeBackend()
        # Pre-populate the fake backend so export_id_mtime returns index_map.
        for doc_id, mtime in index_map.items():
            backend.upsert_many([{"id": doc_id, "mtime": mtime}])
        # Reset upsert log so only verifier-driven writes show up below.
        backend.upserted.clear()

        def fake_index_file_list(_backend, file_pairs, coll_name,
                                 batch_size=50, verbose=False, on_progress=None,
                                 stop_event=None):
            pairs = list(file_pairs)
            indexed.extend(pairs)
            if on_progress:
                on_progress(len(pairs), 0)
            return len(pairs), 0

        with patch("indexserver.verifier.index_file_list", fake_index_file_list):
            run_verify(_cfg, src_root=self.tmpdir, collection="test_coll",
                       delete_orphans=delete_orphans, backend=backend)

        return indexed, backend.deleted

    def test_missing_files_are_indexed(self):
        indexed, _ = self._run_verify_with_backend(index_map={})
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
        indexed, _ = self._run_verify_with_backend(index_map=index_map)
        self.assertEqual(len(indexed), 0)

    def test_stale_file_is_reindexed(self):
        foo_id = file_id("src/foo.cs")
        bar_id = file_id("src/bar.cs")
        index_map = {
            foo_id: self._mtime("src/foo.cs") - 1,  # stale
            bar_id: self._mtime("src/bar.cs"),       # current
        }
        indexed, _ = self._run_verify_with_backend(index_map=index_map)
        rel_paths = [rel for _, rel in indexed]
        self.assertEqual(len(indexed), 1)
        self.assertTrue(any("foo.cs" in r for r in rel_paths))

    def test_only_missing_count_matches_indexed_count(self):
        indexed, _ = self._run_verify_with_backend(index_map={})
        self.assertEqual(len(indexed), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
