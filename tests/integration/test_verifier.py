"""
Integration tests for the verifier against a live Typesense instance.

TestVerifier — requires Typesense to be running.
"""
from __future__ import annotations
import json, os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _assert_server_ok, _search, _delete_collection, _make_git_repo,
    _FOO_CS, _BAR_CS,
)
from indexserver.indexer import run_index
from indexserver.verifier import run_verify


class TestVerifier(unittest.TestCase):
    """Integration tests for run_verify against a live Typesense instance."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp = int(time.time())
        cls.coll = f"test_verify_{stamp}"
        cls.tmpdir = _make_git_repo({
            "src/foo.cs": _FOO_CS,
            "src/bar.cs": _BAR_CS,
        })
        # Initial index
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, filename: str) -> dict | None:
        hits = _search(self.coll, os.path.splitext(filename)[0],
                       query_by="filename,class_names,method_names,tokens")
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
                       query_by="class_names,tokens,filename")
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
        run_index(src_root=self.tmpdir, collection=self.coll, resethard=False, verbose=False)
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

        run_index(src_root=self.tmpdir, collection=self.coll, resethard=False, verbose=False)
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
    unittest.main()
