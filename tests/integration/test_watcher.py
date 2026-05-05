"""
Integration tests for the file watcher.

TestSourceChangeHandlerIntegration — requires Typesense; uses a real IndexQueue.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest, subprocess, tempfile

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import _assert_server_ok, _search, _delete_collection
from indexserver.index_queue import IndexQueue
from indexserver.indexer import build_schema


class TestSourceChangeHandlerIntegration(unittest.TestCase):
    """Integration tests: SourceChangeHandler → IndexQueue → real Typesense collection."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        import typesense as _ts
        from indexserver.config import load_config as _load_config
        _cfg = _load_config()
        stamp = int(time.time())
        cls.coll = f"test_watcher_{stamp}"
        cls.client = _ts.Client(_cfg.typesense_client_config)
        cls.client.collections.create(build_schema(cls.coll))
        cls.tmpdir = tempfile.mkdtemp(prefix="ts_wint_test_")
        subprocess.run(["git", "-C", cls.tmpdir, "init", "-q"], check=True)
        cls.queue = IndexQueue(max_file_bytes=_cfg.max_file_bytes)
        cls.queue.start(cls.client)

    @classmethod
    def tearDownClass(cls):
        cls.queue.stop()
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _make_handler(self):
        from indexserver.watcher import SourceChangeHandler
        from indexserver.config import load_config as _load_config
        return SourceChangeHandler(self.queue, self.tmpdir, self.coll, _load_config())

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
    unittest.main()
