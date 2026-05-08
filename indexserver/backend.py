"""
Tantivy-backed index store. One Tantivy index per source root, identified
by a "collection" name (e.g. ``codesearch_default``).

Three roles a process can play against an index directory:

    Backend(path, write=True)   — index owner; writes, deletes, commits.
                                  Tantivy enforces a single concurrent writer
                                  per directory; the management daemon owns it.
    Backend(path, write=False)  — read-only client; CLIs and other helpers.
    drop(path)                  — remove the index directory entirely (used by
                                  --resethard before recreating).

The backend deliberately does not depend on indexserver.config so that tests
can construct it against a temp directory without loading the real config.
"""

from __future__ import annotations

import gc
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import tantivy


# Schema definition — kept here because the same field set is consulted by
# writer (build_document → backend.add) and reader (search.search).
#
# Fields the user can search by name. The values listed here are also the
# accepted query_by tokens.
SEARCHABLE_FIELDS: tuple[str, ...] = (
    "filename", "namespace",
    "tokens",
    "class_names", "method_names",
    "member_sigs",
    "base_types", "field_types", "local_types",
    "param_types", "return_types", "cast_types",
    "type_refs", "call_sites", "member_accesses",
    "attr_names", "usings",
)

# Multi-value fields (added several times per document).
MULTI_VALUE_FIELDS: frozenset[str] = frozenset({
    "class_names", "method_names",
    "member_sigs",
    "base_types", "field_types", "local_types",
    "param_types", "return_types", "cast_types",
    "type_refs", "call_sites", "member_accesses",
    "attr_names", "usings",
    "path_segments",
})

# Fields stored verbatim and used as exact-match filter terms.
RAW_FIELDS: frozenset[str] = frozenset({
    "id", "relative_path", "extension", "language", "path_segments",
})


def build_schema() -> tantivy.Schema:
    sb = tantivy.SchemaBuilder()
    # Identity / retrieval
    sb.add_text_field("id",            stored=True, tokenizer_name="raw")
    sb.add_text_field("relative_path", stored=True, tokenizer_name="raw")
    sb.add_text_field("filename",      stored=True)
    sb.add_text_field("extension",     stored=True, tokenizer_name="raw")
    sb.add_text_field("language",      stored=True, tokenizer_name="raw")
    sb.add_text_field("namespace",     stored=True)
    sb.add_text_field("path_segments", stored=True, tokenizer_name="raw")

    # Tokens — pre-split bag-of-identifiers from the file body.
    sb.add_text_field("tokens", stored=False)

    # Multi-value pre-extracted fields. The default tokenizer further splits
    # values like "Task<Widget>" into the contained identifiers.
    for f in (
        "class_names", "method_names",
        "member_sigs",
        "base_types", "field_types", "local_types",
        "param_types", "return_types", "cast_types",
        "type_refs", "call_sites", "member_accesses",
        "attr_names", "usings",
    ):
        sb.add_text_field(f, stored=True)

    # File modification time — fast field so export_id_mtime() stays cheap.
    sb.add_unsigned_field("mtime", stored=True, fast=True)
    return sb.build()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

@dataclass
class _OpenFailure:
    path: str
    error: Exception


class Backend:
    """One Tantivy index, with optional writer."""

    HEAP_BYTES = 50_000_000  # 50 MB — default index buffer

    def __init__(self, path: str, write: bool = False, create: bool = True):
        self.path = str(path)
        self._write = write
        self._lock = threading.Lock()
        self._writer: tantivy.IndexWriter | None = None
        self._dirty = False
        # Number of buffered adds + deletes since the last commit. Surfaced
        # in /status so the operator sees progress between commits.
        self._buffered = 0

        os.makedirs(self.path, exist_ok=True)
        # If the directory has no index files yet, create one with our schema.
        # An existing index validates its on-disk schema against ours implicitly.
        if not _has_index_files(self.path):
            if not create and not write:
                raise FileNotFoundError(f"no Tantivy index at {self.path}")
            self._index = tantivy.Index(build_schema(), path=self.path)
        else:
            self._index = tantivy.Index.open(self.path)

        self._schema = self._index.schema
        if write:
            self._writer = self._index.writer(heap_size=self.HEAP_BYTES)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._writer is None:
            return
        # Commit any pending work; failures are logged inside commit().
        if self._dirty:
            try:
                self.commit()
            except Exception:
                # Already logged by commit(); close() must not raise.
                pass
        with self._lock:
            if self._writer is not None:
                try:
                    self._writer.wait_merging_threads()
                except Exception:
                    # Best-effort cleanup; closing the writer below is what matters.
                    pass
                self._writer = None

    @property
    def schema(self) -> tantivy.Schema:
        return self._schema

    def num_documents(self) -> int:
        self._index.reload()
        return self._index.searcher().num_docs

    @property
    def has_pending(self) -> bool:
        """True if there are uncommitted add/delete operations on this writer."""
        return self._dirty

    @property
    def buffered_count(self) -> int:
        """Number of add+delete operations buffered since the last commit."""
        return self._buffered

    def add(self, doc: dict) -> None:
        """Buffer an upsert (delete-by-id + add) on the writer. No commit."""
        if self._writer is None:
            raise RuntimeError("Backend opened read-only; cannot add")
        with self._lock:
            self._writer.delete_documents("id", doc["id"])
            self._writer.add_document(_to_tantivy_doc(doc))
            self._dirty = True
            self._buffered += 1

    def delete(self, doc_id: str) -> None:
        """Buffer a delete on the writer. No commit."""
        if self._writer is None:
            raise RuntimeError("Backend opened read-only; cannot delete")
        with self._lock:
            self._writer.delete_documents("id", doc_id)
            self._dirty = True
            self._buffered += 1

    def commit(self) -> None:
        """Commit buffered changes. On failure, rollback and reopen the writer.

        Raises the underlying exception so callers can react (e.g. requeue).
        Resets ``has_pending`` regardless of outcome — committed items move
        forward; rolled-back items are gone from the writer's memory and the
        caller is expected to requeue them.
        """
        if self._writer is None or not self._dirty:
            return
        with self._lock:
            try:
                self._writer.commit()
                self._dirty = False
                self._buffered = 0
                self._index.reload()
                return
            except Exception as commit_err:
                msg = (
                    f"[backend] commit failed in {self.path}: "
                    f"{type(commit_err).__name__}: {commit_err}"
                )
                print(msg, flush=True)
                try:
                    self._writer.rollback()
                except Exception:
                    # Rollback failure is not actionable; the original commit_err
                    # below is the one we surface, and _reopen_writer() recovers
                    # the writer from any in-between state.
                    pass
                self._dirty = False
                self._buffered = 0
                try:
                    self._reopen_writer()
                    print(f"[backend] reopened writer in {self.path}", flush=True)
                except Exception as reopen_err:
                    print(f"[backend] CRITICAL: writer reopen failed in {self.path}: {reopen_err}", flush=True)
                raise

    def _reopen_writer(self) -> None:
        """Discard the current writer and open a fresh one. Used after a
        commit failure leaves the writer in an undefined state."""
        try:
            self._writer.wait_merging_threads()
        except Exception:
            # The writer is already in an undefined state (we're recovering from
            # a commit failure); skip the wait and rebuild from disk below.
            pass
        self._writer = None
        gc.collect()
        # Reload the index handle so the new writer sees the post-rollback state.
        # If no commit has ever succeeded, meta.json may not exist yet — fall
        # back to creating a fresh index against the same schema.
        if _has_index_files(self.path):
            self._index = tantivy.Index.open(self.path)
        else:
            self._index = tantivy.Index(build_schema(), path=self.path)
        self._schema = self._index.schema
        self._writer = self._index.writer(heap_size=self.HEAP_BYTES)

    # ── writes ───────────────────────────────────────────────────────────────

    def upsert_many(self, docs: list[dict]) -> tuple[int, int]:
        """Add a batch of documents and commit. Convenience wrapper around
        ``add`` + ``commit`` used by tests and the synchronous indexer.

        Returns (n_ok, n_failed). On commit failure all docs are reported as
        failed (Tantivy's commit is atomic — partial application isn't a
        thing here).
        """
        if not docs:
            return 0, 0
        n_added = 0
        for d in docs:
            try:
                self.add(d)
                n_added += 1
            except Exception as e:
                rel = d.get("relative_path", d.get("id", "?"))
                print(f"[backend] add failed for {rel}: {type(e).__name__}: {e}", flush=True)
        try:
            self.commit()
        except Exception:
            return 0, len(docs)
        return n_added, len(docs) - n_added

    def delete_many(self, ids: list[str]) -> int:
        if not ids:
            return 0
        for doc_id in ids:
            self.delete(doc_id)
        try:
            self.commit()
        except Exception:
            return 0
        return len(ids)

    def delete_all(self) -> None:
        if self._writer is None:
            raise RuntimeError("Backend opened read-only; cannot delete")
        with self._lock:
            self._writer.delete_all_documents()
            self._writer.commit()
            self._dirty = False
        self._index.reload()

    # ── reads ────────────────────────────────────────────────────────────────

    def searcher(self) -> tantivy.Searcher:
        self._index.reload()
        return self._index.searcher()

    def export_id_mtime(self) -> dict[str, int]:
        """Return {doc_id: mtime} for every document. Used by the verifier."""
        searcher = self.searcher()
        n = searcher.num_docs
        if n == 0:
            return {}
        result = searcher.search(tantivy.Query.all_query(), limit=n)
        out: dict[str, int] = {}
        for _score, addr in result.hits:
            doc = searcher.doc(addr).to_dict()
            doc_id = (doc.get("id") or [""])[0]
            mtime  = (doc.get("mtime") or [0])[0]
            if doc_id:
                out[doc_id] = int(mtime)
        return out


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def drop(path: str) -> None:
    """Remove the index directory entirely. Used by --resethard."""
    p = Path(path)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def _has_index_files(path: str) -> bool:
    try:
        for entry in os.listdir(path):
            if entry == "meta.json" or entry.endswith(".store"):
                return True
    except FileNotFoundError:
        return False
    return False


def _to_tantivy_doc(d: dict) -> tantivy.Document:
    """Translate a flat dict (build_document output) into a Tantivy Document."""
    doc = tantivy.Document()
    for name, value in d.items():
        if value is None or value == "":
            continue
        if name == "mtime":
            doc.add_unsigned("mtime", int(value))
        elif isinstance(value, list):
            for v in value:
                if v:
                    doc.add_text(name, str(v))
        else:
            doc.add_text(name, str(value))
    return doc


# ---------------------------------------------------------------------------
# Path resolution — where the index for a given root lives on disk.
# ---------------------------------------------------------------------------

def index_dir_for(repo_root: str | Path, collection: str) -> str:
    """Return <repo_root>/.tantivy/<collection>/ (created on demand)."""
    base = Path(repo_root) / ".tantivy" / collection
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def sanitize_collection_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())
