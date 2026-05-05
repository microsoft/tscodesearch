"""
Unit tests for the indexer: extract_metadata, index_file_list, and IndexQueue.

TestExtractCsMetadata — no server; tree-sitter C# extractor unit tests
TestIndexFileList     — no server; shared batch-upsert pipeline (mock client)
TestIndexQueue        — no server; queue enqueue/dedup/mtime/worker behavior (mock client)

Integration tests (require Typesense) are in tests/integration/test_indexer.py.

Run (from WSL):
    ~/.local/indexserver-venv/bin/pytest tests/unit/test_indexer.py -v
"""

import os
import shutil
import tempfile
import time
import unittest

from tests.helpers import (
    _MockTypesenseClient,
    _QUALIFIED_CS, _GENERIC_WRAPPER_CS,
)
from indexserver.indexer import (
    index_file_list, extract_metadata, SourceFile,
)
from indexserver.index_queue import IndexQueue, MTIME_DELETE


# ── TestExtractCsMetadata ─────────────────────────────────────────────────────

class TestExtractCsMetadata(unittest.TestCase):
    """Unit tests for the tree-sitter C# extractor — no server required."""

    def test_class_names(self):
        src = b"namespace N { public class MyClass { } }"
        meta = extract_metadata(src, ".cs")
        self.assertIn("MyClass", meta["class_names"])

    def test_interface_in_base_types(self):
        src = b"public class Impl : IService { }"
        meta = extract_metadata(src, ".cs")
        self.assertIn("IService", meta["base_types"])

    def test_call_sites(self):
        src = b"class C { void M() { Foo.Bar(); Baz(); } }"
        meta = extract_metadata(src, ".cs")
        self.assertTrue(
            "Bar" in meta["call_sites"] or "Baz" in meta["call_sites"],
            f"call_sites: {meta['call_sites']}"
        )

    def test_usings(self):
        src = b"using System; using System.Collections.Generic;"
        meta = extract_metadata(src, ".cs")
        self.assertIn("System", meta["usings"])

    def test_malformed_source_no_crash(self):
        src = b"{{ totally invalid C# !! @@@"
        meta = extract_metadata(src, ".cs")
        self.assertIsInstance(meta, dict)

    def test_qualified_base_type_stripped(self):
        meta = extract_metadata(_QUALIFIED_CS.encode(), ".cs")
        self.assertIn("IBlobStore", meta["base_types"],
                      f"base_types: {meta['base_types']}")
        self.assertNotIn("Acme.IBlobStore", meta["base_types"])

    def test_qualified_type_ref_field_stripped(self):
        meta = extract_metadata(_QUALIFIED_CS.encode(), ".cs")
        self.assertIn("IBlobStore", meta["type_refs"],
                      f"type_refs: {meta['type_refs']}")
        self.assertNotIn("Acme.IBlobStore", meta["type_refs"])

    def test_qualified_attribute_stripped(self):
        meta = extract_metadata(_QUALIFIED_CS.encode(), ".cs")
        self.assertIn("Authorize", meta["attr_names"],
                      f"attr_names: {meta['attr_names']}")
        self.assertNotIn("My.Auth.Authorize", meta["attr_names"])

    def test_type_ref_generic_stores_full_and_arg(self):
        meta = extract_metadata(_GENERIC_WRAPPER_CS.encode(), ".cs")
        refs = meta["type_refs"]
        self.assertIn("IBlobStore", refs,
                      f"IBlobStore (type arg) should appear in type_refs: {refs}")
        self.assertTrue(any("IList" in r for r in refs),
                        f"IList should appear in type_refs: {refs}")

    def test_type_ref_task_generic_stores_arg(self):
        meta = extract_metadata(_GENERIC_WRAPPER_CS.encode(), ".cs")
        self.assertIn("IBlobStore", meta["type_refs"],
                      f"IBlobStore (Task<IBlobStore> return type arg) should be in type_refs: {meta['type_refs']}")


# ── TestIndexFileList ─────────────────────────────────────────────────────────

class TestIndexFileList(unittest.TestCase):
    """Unit tests for index_file_list — the shared batch-upsert pipeline.

    Uses a mock Typesense client; no running server required.
    """

    COLL = "test_coll"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ts_ifl_test_")
        self.mock_client = _MockTypesenseClient(self.COLL)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, rel: str, content: str) -> tuple[str, str]:
        full = os.path.join(self.tmpdir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full, rel

    def test_indexes_all_files(self):
        """All valid files are upserted."""
        f1 = self._make_file("sub/Foo.cs", "class Foo {}")
        f2 = self._make_file("sub/Bar.cs", "class Bar {}")
        total, errors = index_file_list(
            self.mock_client, [f1, f2], self.COLL, batch_size=10,
        )
        self.assertEqual(total, 2)
        self.assertEqual(errors, 0)
        self.assertEqual(
            len(self.mock_client.collections[self.COLL].documents.upserted), 2
        )

    def test_batches_files(self):
        """on_progress is called once per flushed batch."""
        files = [self._make_file(f"sub/File{i}.cs", f"class File{i} {{}}") for i in range(7)]
        calls: list[tuple[int, int]] = []
        total, errors = index_file_list(
            self.mock_client, files, self.COLL,
            batch_size=3, on_progress=lambda n, e: calls.append((n, e)),
        )
        self.assertEqual(total, 7)
        self.assertEqual(errors, 0)
        # batch_size=3: batches of 3, 3, 1 → 3 callbacks
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][0], 7)

    def test_empty_input_returns_zero(self):
        """Empty file pair list returns (0, 0) without calling on_progress."""
        calls: list = []
        total, errors = index_file_list(
            self.mock_client, [], self.COLL, batch_size=50,
            on_progress=lambda n, e: calls.append((n, e)),
        )
        self.assertEqual(total, 0)
        self.assertEqual(errors, 0)
        self.assertEqual(calls, [])

    def test_on_progress_none_no_crash(self):
        """on_progress=None (default) does not raise."""
        f = self._make_file("a.cs", "class A {}")
        total, errors = index_file_list(
            self.mock_client, [f], self.COLL, batch_size=50,
        )
        self.assertEqual(total, 1)
        self.assertEqual(errors, 0)

    def test_unreadable_file_counted_as_error(self):
        """A file that does not exist is counted as an error, not a crash."""
        ghost = (os.path.join(self.tmpdir, "ghost.cs"), "ghost.cs")
        total, errors = index_file_list(
            self.mock_client, [ghost], self.COLL, batch_size=50,
        )
        self.assertEqual(total, 0)
        self.assertEqual(errors, 1)

    def test_progress_increments_monotonically(self):
        """on_progress receives a non-decreasing indexed count."""
        files = [self._make_file(f"m{i}.cs", f"class M{i} {{}}") for i in range(10)]
        counts: list[int] = []
        index_file_list(
            self.mock_client, files, self.COLL,
            batch_size=4, on_progress=lambda n, _e: counts.append(n),
        )
        for a, b in zip(counts, counts[1:]):
            self.assertGreaterEqual(b, a, "on_progress count went backwards")

    def test_final_partial_batch_is_flushed(self):
        """The last batch (< batch_size) is still flushed and counted."""
        files = [self._make_file(f"p{i}.cs", f"class P{i} {{}}") for i in range(5)]
        total, errors = index_file_list(
            self.mock_client, files, self.COLL, batch_size=4,
        )
        # batch_size=4: first batch 4, second batch 1 — all 5 must be indexed
        self.assertEqual(total, 5)
        self.assertEqual(
            len(self.mock_client.collections[self.COLL].documents.upserted), 5
        )

    def test_mixed_valid_and_invalid_files(self):
        """Valid files are indexed; invalid paths are errors; both counted."""
        f1 = self._make_file("good.cs", "class Good {}")
        ghost = (os.path.join(self.tmpdir, "ghost.cs"), "ghost.cs")
        total, errors = index_file_list(
            self.mock_client, [f1, ghost], self.COLL, batch_size=50,
        )
        self.assertEqual(total, 1)
        self.assertEqual(errors, 1)


# ── TestIndexQueue ────────────────────────────────────────────────────────────

class TestIndexQueue(unittest.TestCase):
    """Unit tests for IndexQueue: enqueue/dedup/mtime/stats/worker behavior.

    Uses a mock Typesense client — no running server required.
    """

    COLL = "test_coll"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ts_iq_test_")
        self.mock_client = _MockTypesenseClient(self.COLL)
        self.queue = IndexQueue(batch_size=10)
        self.queue.start(self.mock_client)

    def tearDown(self):
        self.queue.stop(timeout=2)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _file(self, rel: str, content: str = "class T {}") -> tuple[str, str]:
        full = os.path.join(self.tmpdir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full, rel

    def _drain(self, timeout: float = 5.0) -> None:
        t0 = time.time()
        while self.queue.depth > 0 and time.time() - t0 < timeout:
            time.sleep(0.05)
        time.sleep(0.1)

    # ── enqueue behavior ───────────────────────────────────────────────────────

    def test_enqueue_upsert_returns_true(self):
        full, rel = self._file("Foo.cs")
        self.assertTrue(self.queue.enqueue(full, rel, self.COLL))

    def test_enqueue_duplicate_returns_false(self):
        full, rel = self._file("Foo.cs")
        self.queue._stop.set()  # freeze worker so the item stays in the queue
        self.queue.enqueue(full, rel, self.COLL)
        self.assertFalse(self.queue.enqueue(full, rel, self.COLL))

    def test_enqueue_upsert_auto_stats_mtime(self):
        full, rel = self._file("Foo.cs")
        self.queue.enqueue(full, rel, self.COLL, "upsert")
        # Stop the worker so the item stays in the queue
        self.queue._stop.set()
        with self.queue._cond:
            item = next(iter(self.queue._items.values()))
        mtime = item[4]
        self.assertIsNotNone(mtime, "mtime should be auto-statted for upserts")
        self.assertEqual(mtime, int(os.stat(full).st_mtime))

    def test_enqueue_delete_has_mtime_delete_sentinel(self):
        full, rel = self._file("Foo.cs")
        self.queue.enqueue(full, rel, self.COLL, "delete")
        self.queue._stop.set()
        with self.queue._cond:
            item = next(iter(self.queue._items.values()))
        self.assertIs(item[4], MTIME_DELETE)

    def test_enqueue_dedup_updates_mtime(self):
        full, rel = self._file("Foo.cs")
        self.queue.enqueue(full, rel, self.COLL, "upsert")
        # Advance mtime
        new_mtime = int(os.stat(full).st_mtime) + 100
        os.utime(full, (new_mtime, new_mtime))
        self.queue.enqueue(full, rel, self.COLL, "upsert")
        self.queue._stop.set()
        with self.queue._cond:
            item = next(iter(self.queue._items.values()))
        self.assertEqual(item[4], new_mtime)

    def test_depth_reflects_queue_size(self):
        full, rel = self._file("A.cs")
        self.queue._stop.set()  # stop worker from draining
        self.queue.enqueue(full, rel, self.COLL)
        self.assertEqual(self.queue.depth, 1)

    def test_enqueue_bulk_counts(self):
        self.queue._stop.set()
        pairs = [SourceFile(f, r, int(os.stat(f).st_mtime)) for f, r in (self._file(f"f{i}.cs") for i in range(5))]
        n_new, n_dedup = self.queue.enqueue_bulk(pairs, self.COLL)
        self.assertEqual(n_new, 5)
        self.assertEqual(n_dedup, 0)

    def test_enqueue_bulk_dedup(self):
        self.queue._stop.set()
        full, rel = self._file("Dup.cs")
        self.queue.enqueue(full, rel, self.COLL)
        n_new, n_dedup = self.queue.enqueue_bulk([SourceFile(full, rel, int(os.stat(full).st_mtime))], self.COLL)
        self.assertEqual(n_new, 0)
        self.assertEqual(n_dedup, 1)

    # ── stats ──────────────────────────────────────────────────────────────────

    def test_stats_enqueued_counter(self):
        self.queue._stop.set()
        full, rel = self._file("A.cs")
        self.queue.enqueue(full, rel, self.COLL)
        self.assertEqual(self.queue.stats()["enqueued"], 1)

    def test_stats_deduped_counter(self):
        self.queue._stop.set()
        full, rel = self._file("A.cs")
        self.queue.enqueue(full, rel, self.COLL)
        self.queue.enqueue(full, rel, self.COLL)
        self.assertEqual(self.queue.stats()["deduped"], 1)

    def test_stats_has_expected_keys(self):
        stats = self.queue.stats()
        for key in ("depth", "enqueued", "deduped", "upserted", "deleted", "skipped", "errors"):
            self.assertIn(key, stats, f"stats() missing key: {key}")

    # ── worker / flush behavior ────────────────────────────────────────────────

    def test_worker_upserts_to_typesense(self):
        full, rel = self._file("MyFile.cs", "namespace T { public class MyClass {} }")
        self.queue.enqueue(full, rel, self.COLL)
        self._drain()
        docs = self.mock_client.collections[self.COLL].documents.upserted
        self.assertGreater(len(docs), 0)
        self.assertEqual(self.queue.stats()["upserted"], len(docs))

    def test_worker_skips_unchanged_file(self):
        """File whose stored mtime == current mtime must not be re-upserted."""
        from indexserver.indexer import file_id as _file_id
        full, rel = self._file("Skip.cs", "class Skip {}")
        mtime = int(os.stat(full).st_mtime)
        doc_id = _file_id(rel)
        # Pre-populate mock with matching mtime so the skip condition fires
        self.mock_client.collections[self.COLL].documents._stored[doc_id] = {"mtime": mtime}
        initial_count = len(self.mock_client.collections[self.COLL].documents.upserted)

        self.queue.enqueue(full, rel, self.COLL)
        self._drain()

        final_count = len(self.mock_client.collections[self.COLL].documents.upserted)
        self.assertEqual(final_count, initial_count,
                         "Upsert should be skipped when mtime matches stored value")
        self.assertGreater(self.queue.stats()["skipped"], 0)

    def test_worker_deletes_from_typesense(self):
        from indexserver.indexer import file_id as _file_id
        full, rel = self._file("Gone.cs", "class Gone {}")
        doc_id = _file_id(rel)
        # Pre-populate so there is something to delete
        self.mock_client.collections[self.COLL].documents._stored[doc_id] = {"id": doc_id}
        self.queue.enqueue(full, rel, self.COLL, "delete")
        self._drain()
        deleted = self.mock_client.collections[self.COLL].documents.deleted
        self.assertIn(doc_id, deleted)
        self.assertEqual(self.queue.stats()["deleted"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
