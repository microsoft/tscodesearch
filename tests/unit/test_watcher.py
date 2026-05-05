"""
Unit tests for the file watcher: CsChangeHandler event routing and flush logic.

TestCsChangeHandlerUnit — no server needed; uses a lightweight mock queue.

Integration tests (require Typesense) are in tests/integration/test_watcher.py.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest tests/unit/test_watcher.py -v
"""

import os
import shutil
import tempfile
import unittest

from tests.helpers import _FakeEvent


# ── lightweight mock queue ────────────────────────────────────────────────────

class _MockQueue:
    """Records enqueue() calls for assertion without any Typesense interaction."""

    def __init__(self):
        self.calls: list = []  # (full_path, rel, collection, action, mtime)

    def enqueue(self, full_path, rel, collection, action="upsert", mtime=None, reason=""):
        self.calls.append((full_path, rel, collection, action, mtime))
        return True


# ── TestCsChangeHandlerUnit ───────────────────────────────────────────────────

class TestCsChangeHandlerUnit(unittest.TestCase):
    """Unit tests for CsChangeHandler event routing and flush → queue forwarding.

    Uses _MockQueue — no running server, no IndexQueue worker thread.
    """

    COLL = "test_coll"

    def setUp(self):
        from indexserver.watcher import CsChangeHandler
        self.tmpdir = tempfile.mkdtemp(prefix="ts_handler_test_")
        self.queue = _MockQueue()
        self.handler = CsChangeHandler(self.queue, self.tmpdir, collection=self.COLL)

    def tearDown(self):
        if self.handler._timer:
            self.handler._timer.cancel()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cs_file(self, name: str = "Test.cs", content: str = "class Test {}") -> str:
        path = os.path.join(self.tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # ── event routing ──────────────────────────────────────────────────────────

    def test_on_created_cs_adds_upsert(self):
        path = self._cs_file()
        self.handler.on_created(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(path), "created")

    def test_on_modified_cs_adds_upsert(self):
        path = self._cs_file()
        self.handler.on_modified(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(path), "modified")

    def test_on_deleted_cs_adds_delete(self):
        path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler.on_deleted(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(path), "deleted")

    def test_on_created_non_indexed_ext_ignored(self):
        path = os.path.join(self.tmpdir, "build.log")
        self.handler.on_created(_FakeEvent(path))
        with self.handler._lock:
            self.assertNotIn(path, self.handler._pending)

    def test_on_created_directory_ignored(self):
        path = os.path.join(self.tmpdir, "subdir")
        self.handler.on_created(_FakeEvent(path, is_directory=True))
        with self.handler._lock:
            self.assertEqual(len(self.handler._pending), 0)

    def test_on_created_excluded_dir_skipped(self):
        excluded = os.path.join(self.tmpdir, "Target", "x64", "debug", "Foo.cs")
        self.handler.on_created(_FakeEvent(excluded))
        with self.handler._lock:
            self.assertNotIn(excluded, self.handler._pending)

    def test_on_modified_deduplicates_same_file(self):
        path = self._cs_file()
        self.handler.on_modified(_FakeEvent(path))
        self.handler.on_modified(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(list(self.handler._pending.keys()), [path])

    def test_on_moved_deletes_old_upserts_new(self):
        old_path = os.path.join(self.tmpdir, "Old.cs")
        new_path = self._cs_file("New.cs")
        self.handler.on_moved(_FakeEvent(old_path, dest_path=new_path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(old_path), "deleted")
            self.assertEqual(self.handler._pending.get(new_path), "created")

    def test_on_moved_to_non_indexed_ext_only_deletes(self):
        old_path = os.path.join(self.tmpdir, "Foo.cs")
        new_path = os.path.join(self.tmpdir, "Foo.log")
        self.handler.on_moved(_FakeEvent(old_path, dest_path=new_path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(old_path), "deleted")
            self.assertNotIn(new_path, self.handler._pending)

    def test_on_moved_from_non_indexed_ext_only_upserts(self):
        old_path = os.path.join(self.tmpdir, "Foo.log")
        new_path = self._cs_file("Foo.cs")
        self.handler.on_moved(_FakeEvent(old_path, dest_path=new_path))
        with self.handler._lock:
            self.assertNotIn(old_path, self.handler._pending)
            self.assertEqual(self.handler._pending.get(new_path), "created")

    # ── debounce timer ─────────────────────────────────────────────────────────

    def test_debounce_timer_is_started(self):
        path = self._cs_file()
        self.handler.on_created(_FakeEvent(path))
        self.assertIsNotNone(self.handler._timer)

    def test_debounce_timer_reset_on_second_event(self):
        path1 = self._cs_file("A.cs")
        path2 = self._cs_file("B.cs")
        self.handler.on_created(_FakeEvent(path1))
        first_timer = self.handler._timer
        self.handler.on_created(_FakeEvent(path2))
        self.assertIsNot(self.handler._timer, first_timer)

    # ── flush → queue forwarding ───────────────────────────────────────────────

    def test_flush_upsert_calls_enqueue(self):
        path = self._cs_file()
        self.handler._pending[path] = "created"
        self.handler._flush()
        actions = [c[3] for c in self.queue.calls]
        self.assertIn("upsert", actions)

    def test_flush_delete_calls_enqueue_with_delete_action(self):
        path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler._pending[path] = "deleted"
        self.handler._flush()
        actions = [c[3] for c in self.queue.calls]
        self.assertIn("delete", actions)

    def test_flush_clears_pending(self):
        path = self._cs_file()
        self.handler._pending[path] = "created"
        self.handler._flush()
        with self.handler._lock:
            self.assertEqual(len(self.handler._pending), 0)

    def test_flush_empty_pending_is_noop(self):
        self.handler._flush()
        self.assertEqual(len(self.queue.calls), 0)

    def test_flush_passes_relative_path(self):
        sub = os.path.join(self.tmpdir, "sub")
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, "Widget.cs")
        with open(path, "w") as f:
            f.write("class Widget {}")
        self.handler._pending[path] = "modified"
        self.handler._flush()
        rels = [c[1] for c in self.queue.calls]
        self.assertTrue(
            any("Widget.cs" in r and not os.path.isabs(r) for r in rels),
            f"Expected relative path in enqueue call, got: {rels}",
        )

    def test_flush_multiple_files_all_enqueued(self):
        paths = [self._cs_file(f"File{i}.cs", f"class File{i} {{}}") for i in range(3)]
        for p in paths:
            self.handler._pending[p] = "modified"
        self.handler._flush()
        self.assertEqual(len(self.queue.calls), 3)

    def test_flush_mixed_actions_forwarded_correctly(self):
        upsert_path = self._cs_file("Keep.cs")
        delete_path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler._pending[upsert_path] = "created"
        self.handler._pending[delete_path] = "deleted"
        self.handler._flush()
        actions = {c[3] for c in self.queue.calls}
        self.assertIn("upsert", actions)
        self.assertIn("delete", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
