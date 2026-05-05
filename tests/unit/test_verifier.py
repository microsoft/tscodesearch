"""
Unit tests for the verifier: _export_index and run_verify.

TestExportIndex      — unit tests for _export_index (no server needed, mocks HTTP)
TestRunVerifyUnit    — unit tests for run_verify (no server, mock index_file_list)

Integration tests (require Typesense) are in tests/integration/test_verifier.py.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest tests/unit/test_verifier.py -v
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tests.helpers import _FOO_CS, _BAR_CS
from indexserver.config import load_config as _load_config
from indexserver.indexer import file_id
from indexserver.verifier import _export_index, run_verify

_cfg = _load_config()


# ── TestExportIndex ───────────────────────────────────────────────────────────

class TestExportIndex(unittest.TestCase):
    """Unit tests for _export_index — no Typesense server needed."""

    def _make_response(self, docs: list[dict]) -> io.BytesIO:
        """Build a fake JSONL HTTP response body."""
        lines = "\n".join(json.dumps(d) for d in docs)
        return io.BytesIO(lines.encode("utf-8"))

    def _patch_urlopen(self, docs: list[dict]):
        """Context manager: patch urllib.request.urlopen to return fake docs."""
        buf = self._make_response(docs)
        mock_cm = MagicMock()
        mock_cm.__enter__ = lambda s: buf
        mock_cm.__exit__ = MagicMock(return_value=False)
        return patch("indexserver.verifier.urllib.request.urlopen",
                     return_value=mock_cm)

    def test_returns_id_mtime_dict(self):
        docs = [{"id": "abc", "mtime": 1700000000}]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(result, {"abc": 1700000000})

    def test_multiple_docs(self):
        docs = [
            {"id": "a", "mtime": 100},
            {"id": "b", "mtime": 200},
            {"id": "c", "mtime": 300},
        ]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(len(result), 3)
        self.assertEqual(result["b"], 200)

    def test_missing_mtime_defaults_to_zero(self):
        docs = [{"id": "no_mtime"}]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(result["no_mtime"], 0)

    def test_doc_without_id_skipped(self):
        docs = [{"content": "no id here"}, {"id": "valid", "mtime": 42}]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(list(result.keys()), ["valid"])

    def test_empty_response_returns_empty_dict(self):
        with self._patch_urlopen([]):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(result, {})

    def test_network_error_returns_empty_dict(self):
        with patch("indexserver.verifier.urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            result = _export_index("test_coll", _cfg)
        self.assertEqual(result, {})


# ── TestRunVerifyUnit ─────────────────────────────────────────────────────────

class TestRunVerifyUnit(unittest.TestCase):
    """Unit tests for run_verify logic — no server; mock _export_index and index_file_list."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ts_verify_unit_")
        # Write two source files
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

    def _run_verify_mocked(self, index_map: dict, delete_orphans: bool = True):
        """Run run_verify with _export_index and index_file_list mocked."""
        indexed = []
        deleted = []

        def fake_index_file_list(client, file_pairs, coll_name,
                                 batch_size=50, verbose=False, on_progress=None,
                                 stop_event=None):
            pairs = list(file_pairs)
            indexed.extend(pairs)
            if on_progress:
                on_progress(len(pairs), 0)
            return len(pairs), 0

        with patch("indexserver.verifier._export_index", return_value=index_map), \
             patch("indexserver.verifier.index_file_list", fake_index_file_list), \
             patch("indexserver.verifier.get_client", return_value=MagicMock()) as mock_client:
            # Patch delete on the mock client
            mock_client.return_value.collections.__getitem__ = MagicMock(
                return_value=MagicMock(
                    documents=MagicMock(
                        __getitem__=lambda s, doc_id: MagicMock(
                            delete=lambda: deleted.append(doc_id)
                        )
                    )
                )
            )
            run_verify(_cfg, src_root=self.tmpdir, collection="test_coll",
                       delete_orphans=delete_orphans)

        return indexed, deleted

    def test_missing_files_are_indexed(self):
        """Files on disk but not in index should be fed to index_file_list."""
        indexed, _ = self._run_verify_mocked(index_map={})
        rel_paths = [rel for _, rel in indexed]
        # Both files should be queued for indexing
        self.assertTrue(any("foo.cs" in r for r in rel_paths))
        self.assertTrue(any("bar.cs" in r for r in rel_paths))

    def test_up_to_date_files_not_reindexed(self):
        """Files with matching mtimes should NOT be re-indexed."""
        foo_id = file_id("src/foo.cs")
        bar_id = file_id("src/bar.cs")
        index_map = {
            foo_id: self._mtime("src/foo.cs"),
            bar_id: self._mtime("src/bar.cs"),
        }
        indexed, _ = self._run_verify_mocked(index_map=index_map)
        self.assertEqual(len(indexed), 0)

    def test_stale_file_is_reindexed(self):
        """A file with a changed mtime should be re-indexed."""
        foo_id = file_id("src/foo.cs")
        bar_id = file_id("src/bar.cs")
        index_map = {
            foo_id: self._mtime("src/foo.cs") - 1,  # stale
            bar_id: self._mtime("src/bar.cs"),        # current
        }
        indexed, _ = self._run_verify_mocked(index_map=index_map)
        rel_paths = [rel for _, rel in indexed]
        self.assertEqual(len(indexed), 1)
        self.assertTrue(any("foo.cs" in r for r in rel_paths))

    def test_only_missing_count_matches_indexed_count(self):
        """When index is empty, missing count == number of files on disk."""
        indexed, _ = self._run_verify_mocked(index_map={})
        # We wrote 2 files
        self.assertEqual(len(indexed), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
