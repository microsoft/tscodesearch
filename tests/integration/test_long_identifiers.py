"""
End-to-end tests for long-identifier and snake_case handling.

Three independent things being verified, all against a real Tantivy index
opened on a temp directory:

  1. ``extract_metadata`` preserves long identifiers verbatim in
     per-identifier multi-value fields -- no truncation, no underscore split.
  2. The backend, indexed via the same path build_document -> backend.add
     uses in production, can store and retrieve documents that contain
     identifiers far longer than Tantivy's default 40-char tokenizer limit.
  3. Querying via ``search()`` with a 50-char identifier returns the file
     whose source contains that identifier, and snake_case names like
     ``add_text_field`` are stored as a single token (a search for ``add``
     against ``class_names`` does NOT find ``add_text_field``).
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import make_test_backend
from indexserver.indexer import build_document, extract_metadata
from indexserver.search import search as _search


# A 50-char C# identifier -- longer than Tantivy's 40-char tokenizer limit.
_LONG_CLASS = "InitializeNotificationHistoryAcrossDataCentersUSA"
assert len(_LONG_CLASS) == 49

# A 60-char method name on the same class.
_LONG_METHOD = "ScheduleReplicationToAllSecondaryDataCentersWithRetryAndAck"
assert len(_LONG_METHOD) == 59

# Snake_case identifier that the default tokenizer would split into 3 tokens.
_SNAKE_CLASS = "add_text_field"


def _make_source() -> bytes:
    return (f"""namespace Sample {{
    public class {_LONG_CLASS} {{
        public void {_LONG_METHOD}() {{ }}
    }}

    public class {_SNAKE_CLASS} {{
        public void DoNothing() {{ }}
    }}
}}
""").encode()


# -- Metadata extraction (no backend) ------------------------------------------

class TestExtractMetadataPreservesLongIdentifiers(unittest.TestCase):
    """flat_from_fd / extract_metadata stores long identifiers verbatim."""

    def setUp(self):
        self.meta = extract_metadata(_make_source(), ".cs")

    def test_class_names_keep_full_long_name(self):
        self.assertIn(_LONG_CLASS, self.meta["class_names"])

    def test_method_names_keep_full_long_name(self):
        self.assertIn(_LONG_METHOD, self.meta["method_names"])

    def test_snake_case_class_name_is_single_entry(self):
        self.assertIn(_SNAKE_CLASS, self.meta["class_names"])
        # Each entry stays a single identifier -- the indexer must NOT split
        # snake_case into pieces.
        self.assertNotIn("add", self.meta["class_names"])
        self.assertNotIn("text", self.meta["class_names"])
        self.assertNotIn("field", self.meta["class_names"])

    def test_tokens_is_a_list_of_full_identifiers(self):
        tokens = self.meta["tokens"]
        self.assertIsInstance(tokens, list,
            "tokens should be a multi-value list, not a space-joined string")
        self.assertIn(_LONG_CLASS, tokens)
        self.assertIn(_LONG_METHOD, tokens)
        self.assertIn(_SNAKE_CLASS, tokens)


# -- Backend round-trip (real Tantivy index) ----------------------------------

class TestBackendStoresLongIdentifiers(unittest.TestCase):
    """A real Tantivy backend accepts and returns long identifier values."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ts_longid_test_")
        self.backend, self._cleanup = make_test_backend()
        self.src_path = os.path.join(self.workdir, "Sample.cs")
        with open(self.src_path, "wb") as f:
            f.write(_make_source())
        doc = build_document(self.src_path, "Sample.cs")
        self.assertIsNotNone(doc, "build_document must succeed on a real file")
        self.backend.upsert_many([doc])

    def tearDown(self):
        self._cleanup()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_indexed_document_count(self):
        self.assertEqual(self.backend.num_documents(), 1)

    def test_search_finds_file_by_long_class_name(self):
        result = _search(self.backend,
                         q=_LONG_CLASS,
                         query_by="class_names,tokens,path_tokens",
                         per_page=10)
        hits = result.get("hits", [])
        self.assertEqual(len(hits), 1,
            f"Expected one hit for {_LONG_CLASS}, got {len(hits)}")
        self.assertEqual(hits[0]["document"]["relative_path"], "Sample.cs")

    def test_search_finds_file_by_long_method_name(self):
        result = _search(self.backend,
                         q=_LONG_METHOD,
                         query_by="method_names,tokens",
                         per_page=10)
        hits = result.get("hits", [])
        self.assertEqual(len(hits), 1,
            f"Expected one hit for {_LONG_METHOD}, got {len(hits)}")

    def test_snake_case_is_one_token_not_three(self):
        # A search for ``add_text_field`` finds the file (whole identifier).
        hit = _search(self.backend, q=_SNAKE_CLASS,
                      query_by="class_names", per_page=5)
        self.assertEqual(len(hit.get("hits", [])), 1)

        # A search for just ``add`` against ``class_names`` does NOT match,
        # because raw tokenizer kept ``add_text_field`` as a single term.
        partial = _search(self.backend, q="add",
                          query_by="class_names", per_page=5)
        self.assertEqual(len(partial.get("hits", [])), 0,
            "Snake-case identifier was incorrectly split: 'add' should not "
            "match 'add_text_field' in class_names with raw tokenizer.")


# -- Full daemon pipeline: index + search + AST -------------------------------

class TestQueryCodebasePipelineFindsLongIdentifier(unittest.TestCase):
    """The full /query-codebase pipeline (Tantivy pre-filter -> AST post-filter)
    returns the file containing a long identifier when the caller passes the
    full long pattern."""

    def setUp(self):
        # Real backend + real source file. Mirror what tsquery_server does on
        # /query-codebase, but in-process for the test.
        self.workdir = tempfile.mkdtemp(prefix="ts_longid_e2e_")
        self.backend, self._cleanup = make_test_backend()
        self.src_path = os.path.join(self.workdir, "Sample.cs")
        with open(self.src_path, "wb") as f:
            f.write(_make_source())
        doc = build_document(self.src_path, "Sample.cs")
        self.backend.upsert_many([doc])

    def tearDown(self):
        self._cleanup()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_declarations_mode_finds_long_class(self):
        # Mirror the daemon's `declarations`/default resolver and the AST
        # post-filter exactly.
        from indexserver.search_modes import resolve_query_params
        from query.dispatch import query_file

        query_by, weights = resolve_query_params("symbols", "", "")
        result = _search(self.backend, q=_LONG_CLASS,
                         query_by=query_by, weights=weights, per_page=10)
        hits = result.get("hits", [])
        self.assertEqual(len(hits), 1,
            f"Tantivy pre-filter found {len(hits)} files, expected 1")

        # AST post-filter on the file content.
        with open(self.src_path, "rb") as f:
            src_bytes = f.read()
        ast_matches = query_file(src_bytes, ".cs", "declarations", _LONG_CLASS)
        self.assertGreater(len(ast_matches), 0,
            f"AST did not find {_LONG_CLASS} declaration in source")

    def test_calls_mode_finds_long_method(self):
        # Add a caller of the long method in a second file.
        caller_src = (
            f"namespace Sample {{\n"
            f"    public class Caller {{\n"
            f"        public void Invoke({_LONG_CLASS} obj) {{ obj.{_LONG_METHOD}(); }}\n"
            f"    }}\n"
            f"}}\n"
        ).encode()
        caller_path = os.path.join(self.workdir, "Caller.cs")
        with open(caller_path, "wb") as f:
            f.write(caller_src)
        self.backend.upsert_many([build_document(caller_path, "Caller.cs")])

        from indexserver.search_modes import resolve_query_params
        from query.dispatch import query_file

        query_by, weights = resolve_query_params("calls", "", "")
        result = _search(self.backend, q=_LONG_METHOD,
                         query_by=query_by, weights=weights, per_page=10)
        hits = result.get("hits", [])
        self.assertEqual(len(hits), 1,
            f"Expected one calls hit for {_LONG_METHOD}, got {len(hits)}: "
            f"{[h['document']['relative_path'] for h in hits]}")
        self.assertEqual(hits[0]["document"]["relative_path"], "Caller.cs")

        # AST post-filter confirms the call site.
        with open(caller_path, "rb") as f:
            src_bytes = f.read()
        ast_matches = query_file(src_bytes, ".cs", "calls", _LONG_METHOD)
        self.assertGreater(len(ast_matches), 0,
            f"AST did not find call to {_LONG_METHOD} in Caller.cs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
