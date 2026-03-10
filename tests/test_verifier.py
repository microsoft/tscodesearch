"""
Tests for the verifier: _export_index, run_verify, and MCP verify_index tool.

TestExportIndex      — unit tests for _export_index (no server needed, mocks HTTP)
TestRunVerifyUnit    — unit tests for run_verify (no server, mock index_file_list)
TestVerifier         — integration tests (requires Typesense)

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest codesearch/tests/test_verifier.py -v
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _server_ok, _search, _delete_collection, _make_git_repo,
    _FOO_CS, _BAR_CS,
)
from indexserver.indexer import run_index, file_id
from indexserver.verifier import _export_index, run_verify


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
            result = _export_index("test_coll")
        self.assertEqual(result, {"abc": 1700000000})

    def test_multiple_docs(self):
        docs = [
            {"id": "a", "mtime": 100},
            {"id": "b", "mtime": 200},
            {"id": "c", "mtime": 300},
        ]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll")
        self.assertEqual(len(result), 3)
        self.assertEqual(result["b"], 200)

    def test_missing_mtime_defaults_to_zero(self):
        docs = [{"id": "no_mtime"}]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll")
        self.assertEqual(result["no_mtime"], 0)

    def test_doc_without_id_skipped(self):
        docs = [{"content": "no id here"}, {"id": "valid", "mtime": 42}]
        with self._patch_urlopen(docs):
            result = _export_index("test_coll")
        self.assertEqual(list(result.keys()), ["valid"])

    def test_empty_response_returns_empty_dict(self):
        with self._patch_urlopen([]):
            result = _export_index("test_coll")
        self.assertEqual(result, {})

    def test_network_error_returns_empty_dict(self):
        with patch("indexserver.verifier.urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            result = _export_index("test_coll")
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
            run_verify(src_root=self.tmpdir, collection="test_coll",
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


# ── TestVerifier ──────────────────────────────────────────────────────────────

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestVerifier(unittest.TestCase):
    """Integration tests for run_verify against a live Typesense instance."""

    @classmethod
    def setUpClass(cls):
        stamp = int(time.time())
        cls.coll = f"test_verify_{stamp}"
        cls.tmpdir = _make_git_repo({
            "src/foo.cs": _FOO_CS,
            "src/bar.cs": _BAR_CS,
        })
        # Initial index
        run_index(src_root=cls.tmpdir, collection=cls.coll, reset=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, filename: str) -> dict | None:
        hits = _search(self.coll, os.path.splitext(filename)[0],
                       query_by="filename,symbols,class_names,method_names,content")
        return next((h for h in hits if h["filename"] == filename), None)

    # ── no-op: index is already up to date ────────────────────────────────────

    def test_no_changes_updates_nothing(self):
        """Verifying a fresh index should touch zero files."""
        # The index was just built; run verify and confirm nothing changes.
        # We measure by checking that foo.cs is still searchable after verify.
        run_verify(src_root=self.tmpdir, collection=self.coll)
        time.sleep(0.2)
        foo = self._get("foo.cs")
        self.assertIsNotNone(foo)

    # ── missing file added ─────────────────────────────────────────────────────

    def test_missing_file_added_after_verify(self):
        """A new file added to disk but not indexed should appear after verify."""
        new_file = os.path.join(self.tmpdir, "src", "new_widget.cs")
        try:
            with open(new_file, "w", encoding="utf-8") as f:
                f.write("namespace Test { public class NewWidget {} }\n")
            run_verify(src_root=self.tmpdir, collection=self.coll)
            time.sleep(0.5)
            hit = self._get("new_widget.cs")
            self.assertIsNotNone(hit, "new_widget.cs should be in index after verify")
        finally:
            if os.path.exists(new_file):
                os.unlink(new_file)
            # Clean up orphan from index
            run_verify(src_root=self.tmpdir, collection=self.coll, delete_orphans=True)
            time.sleep(0.3)

    # ── stale file reindexed ───────────────────────────────────────────────────

    def test_stale_file_reindexed_after_verify(self):
        """A file modified on disk (newer mtime) should be re-indexed by verify."""
        foo_path = os.path.join(self.tmpdir, "src", "foo.cs")
        # Overwrite with new content and bump mtime
        new_content = "namespace TestNs { public class FooModified {} }\n"
        with open(foo_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        run_verify(src_root=self.tmpdir, collection=self.coll)
        time.sleep(0.5)

        hits = _search(self.coll, "FooModified",
                       query_by="class_names,content,filename")
        names = [h["filename"] for h in hits]
        self.assertIn("foo.cs", names, "Modified foo.cs should be re-indexed")

        # Restore original content for other tests
        with open(foo_path, "w", encoding="utf-8") as f:
            f.write(_FOO_CS)

    # ── orphan deletion ────────────────────────────────────────────────────────

    def test_delete_orphans_removes_deleted_file(self):
        """A file deleted from disk should be removed from the index by verify."""
        orphan_path = os.path.join(self.tmpdir, "src", "orphan.cs")
        with open(orphan_path, "w", encoding="utf-8") as f:
            f.write("namespace Test { public class Orphan {} }\n")

        # Index the orphan file first
        run_index(src_root=self.tmpdir, collection=self.coll, reset=False, verbose=False)
        time.sleep(0.3)

        # Confirm it's in the index
        hit_before = self._get("orphan.cs")
        self.assertIsNotNone(hit_before, "orphan.cs should be indexed before deletion")

        # Delete the file and verify
        os.unlink(orphan_path)
        run_verify(src_root=self.tmpdir, collection=self.coll, delete_orphans=True)
        time.sleep(0.5)

        hit_after = self._get("orphan.cs")
        self.assertIsNone(hit_after, "orphan.cs should be removed from index after verify")

    def test_delete_orphans_false_preserves_entry(self):
        """With delete_orphans=False, deleted files should remain in the index."""
        orphan_path = os.path.join(self.tmpdir, "src", "kept_orphan.cs")
        with open(orphan_path, "w", encoding="utf-8") as f:
            f.write("namespace Test { public class KeptOrphan {} }\n")

        run_index(src_root=self.tmpdir, collection=self.coll, reset=False, verbose=False)
        time.sleep(0.3)

        hit_before = self._get("kept_orphan.cs")
        self.assertIsNotNone(hit_before, "kept_orphan.cs should be indexed before deletion")

        os.unlink(orphan_path)
        run_verify(src_root=self.tmpdir, collection=self.coll, delete_orphans=False)
        time.sleep(0.3)

        hit_after = self._get("kept_orphan.cs")
        self.assertIsNotNone(hit_after,
                             "kept_orphan.cs should still be in index when delete_orphans=False")

        # Clean up: remove the orphan properly
        run_verify(src_root=self.tmpdir, collection=self.coll, delete_orphans=True)
        time.sleep(0.3)

    # ── progress file ──────────────────────────────────────────────────────────

    def test_progress_file_written_on_verify(self):
        """run_verify should write verifier_progress.json with status=complete."""
        import pathlib
        run_dir = pathlib.Path(os.environ.get(
            "TYPESENSE_DATA",
            pathlib.Path.home() / ".local" / "typesense"
        ))
        progress_file = run_dir / "verifier_progress.json"

        run_verify(src_root=self.tmpdir, collection=self.coll)

        self.assertTrue(progress_file.exists(), "verifier_progress.json should be written")
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("status"), "complete")
        self.assertEqual(data.get("collection"), self.coll)


if __name__ == "__main__":
    unittest.main(verbosity=2)
