"""
Tests for the file watcher: CsChangeHandler event routing and flush logic.

TestCsChangeHandlerUnit — no server needed; uses a lightweight mock queue.
TestCsChangeHandlerIntegration — requires Typesense; uses a real IndexQueue.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest codesearch/tests/test_watcher.py -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import (
    _server_ok, _search, _delete_collection, _FakeEvent,
)
from indexserver.index_queue import IndexQueue, MTIME_DELETE
from indexserver.indexer import build_schema


# ── lightweight mock queue ────────────────────────────────────────────────────

class _MockQueue:
    """Records enqueue() calls for assertion without any Typesense interaction."""

    def __init__(self):
        self.calls: list = []  # (full_path, rel, collection, action, mtime)

    def enqueue(self, full_path, rel, collection, action="upsert", mtime=None):
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
            self.assertEqual(self.handler._pending.get(path), "upsert")

    def test_on_modified_cs_adds_upsert(self):
        path = self._cs_file()
        self.handler.on_modified(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(path), "upsert")

    def test_on_deleted_cs_adds_delete(self):
        path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler.on_deleted(_FakeEvent(path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(path), "delete")

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
            self.assertEqual(self.handler._pending.get(old_path), "delete")
            self.assertEqual(self.handler._pending.get(new_path), "upsert")

    def test_on_moved_to_non_indexed_ext_only_deletes(self):
        old_path = os.path.join(self.tmpdir, "Foo.cs")
        new_path = os.path.join(self.tmpdir, "Foo.log")
        self.handler.on_moved(_FakeEvent(old_path, dest_path=new_path))
        with self.handler._lock:
            self.assertEqual(self.handler._pending.get(old_path), "delete")
            self.assertNotIn(new_path, self.handler._pending)

    def test_on_moved_from_non_indexed_ext_only_upserts(self):
        old_path = os.path.join(self.tmpdir, "Foo.log")
        new_path = self._cs_file("Foo.cs")
        self.handler.on_moved(_FakeEvent(old_path, dest_path=new_path))
        with self.handler._lock:
            self.assertNotIn(old_path, self.handler._pending)
            self.assertEqual(self.handler._pending.get(new_path), "upsert")

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
        self.handler._pending[path] = "upsert"
        self.handler._flush()
        actions = [c[3] for c in self.queue.calls]
        self.assertIn("upsert", actions)

    def test_flush_delete_calls_enqueue_with_delete_action(self):
        path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler._pending[path] = "delete"
        self.handler._flush()
        actions = [c[3] for c in self.queue.calls]
        self.assertIn("delete", actions)

    def test_flush_clears_pending(self):
        path = self._cs_file()
        self.handler._pending[path] = "upsert"
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
        self.handler._pending[path] = "upsert"
        self.handler._flush()
        rels = [c[1] for c in self.queue.calls]
        self.assertTrue(
            any("Widget.cs" in r and not os.path.isabs(r) for r in rels),
            f"Expected relative path in enqueue call, got: {rels}",
        )

    def test_flush_multiple_files_all_enqueued(self):
        paths = [self._cs_file(f"File{i}.cs", f"class File{i} {{}}") for i in range(3)]
        for p in paths:
            self.handler._pending[p] = "upsert"
        self.handler._flush()
        self.assertEqual(len(self.queue.calls), 3)

    def test_flush_mixed_actions_forwarded_correctly(self):
        upsert_path = self._cs_file("Keep.cs")
        delete_path = os.path.join(self.tmpdir, "Gone.cs")
        self.handler._pending[upsert_path] = "upsert"
        self.handler._pending[delete_path] = "delete"
        self.handler._flush()
        actions = {c[3] for c in self.queue.calls}
        self.assertIn("upsert", actions)
        self.assertIn("delete", actions)


# ── TestCsChangeHandlerIntegration ───────────────────────────────────────────

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestCsChangeHandlerIntegration(unittest.TestCase):
    """Integration tests: CsChangeHandler → IndexQueue → real Typesense collection."""

    @classmethod
    def setUpClass(cls):
        import typesense as _ts
        from indexserver.config import TYPESENSE_CLIENT_CONFIG
        stamp = int(time.time())
        cls.coll = f"test_watcher_{stamp}"
        cls.client = _ts.Client(TYPESENSE_CLIENT_CONFIG)
        cls.client.collections.create(build_schema(cls.coll))
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_wint_test_")
        subprocess.run(["git", "-C", cls.tmpdir, "init", "-q"], check=True)
        cls.queue = IndexQueue()
        cls.queue.start(cls.client)

    @classmethod
    def tearDownClass(cls):
        cls.queue.stop()
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _make_handler(self):
        from indexserver.watcher import CsChangeHandler
        return CsChangeHandler(self.queue, self.tmpdir, collection=self.coll)

    def _write(self, name: str, content: str) -> str:
        path = os.path.join(self.tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _drain(self, timeout: float = 5.0) -> None:
        """Wait until the queue is drained and Typesense has settled."""
        t0 = time.time()
        while self.queue.depth > 0 and time.time() - t0 < timeout:
            time.sleep(0.05)
        time.sleep(0.2)

    def test_flush_indexes_new_file(self):
        handler = self._make_handler()
        path = self._write("sub/Widget.cs", "namespace Sub { public class Widget {} }")
        handler._pending[path] = "upsert"
        handler._flush()
        self._drain()
        hits = _search(self.coll, "Widget")
        self.assertIn("Widget.cs", [h["filename"] for h in hits])

    def test_flush_updates_modified_file(self):
        handler = self._make_handler()
        path = self._write("sub/Gadget.cs", "namespace Sub { public class GadgetOld {} }")
        handler._pending[path] = "upsert"
        handler._flush()
        self._drain()

        # Overwrite + bump mtime so mtime check doesn't skip it
        with open(path, "w", encoding="utf-8") as f:
            f.write("namespace Sub { public class GadgetNew {} }")
        new_mtime = int(os.stat(path).st_mtime) + 2
        os.utime(path, (new_mtime, new_mtime))
        handler._pending[path] = "upsert"
        handler._flush()
        self._drain()

        hits = _search(self.coll, "GadgetNew", query_by="class_names,tokens")
        self.assertIn("Gadget.cs", [h["filename"] for h in hits])

    def test_flush_removes_deleted_file(self):
        handler = self._make_handler()
        path = self._write("sub/Ephemeral.cs", "public class Ephemeral {}")
        handler._pending[path] = "upsert"
        handler._flush()
        self._drain()
        self.assertTrue(any(h["filename"] == "Ephemeral.cs"
                            for h in _search(self.coll, "Ephemeral")),
                        "File should be indexed before deletion")

        handler._pending[path] = "delete"
        handler._flush()
        self._drain()
        self.assertFalse(any(h["filename"] == "Ephemeral.cs"
                             for h in _search(self.coll, "Ephemeral")),
                         "File should be removed after deletion")


if __name__ == "__main__":
    unittest.main(verbosity=2)
