"""
Tantivy-backed index store. One Tantivy index per source root, identified
by a "collection" name (e.g. ``codesearch_default``).

Three roles a process can play against an index directory:

    Backend(path, write=True)   -- index owner; writes, deletes, commits.
                                  Tantivy enforces a single concurrent writer
                                  per directory; the management daemon owns it.
    Backend(path, write=False)  -- read-only client; CLIs and other helpers.
    drop(path)                  -- remove the index directory entirely (used by
                                  --resethard before recreating).

The backend deliberately does not depend on indexserver.config so that tests
can construct it against a temp directory without loading the real config.
"""

from __future__ import annotations

import gc
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import tantivy


# -- Transient Windows IO retry ------------------------------------------------
#
# On Windows, ``IndexWriter.commit()`` occasionally fails with
# ``Access is denied`` / ``PermissionDenied`` when opening a new segment
# file for write -- even for a freshly-created collection directory.
# Suspected causes include Windows Defender scanning newly created files,
# stale mmap handles from earlier reader Backends releasing asynchronously
# in the same process, and the OS taking a moment to make a just-deleted
# directory's namespace available again. The errors are transient: a brief
# settle delay followed by re-running the same write succeeds. Retry only
# at the ``upsert_many`` level because the writer's in-memory buffer is
# discarded on a failed commit, so we have to re-add the docs.

_COMMIT_RETRY_ATTEMPTS = 4
_COMMIT_RETRY_BASE_DELAY = 0.2  # seconds; doubled each attempt


def _is_transient_windows_io_error(err: BaseException) -> bool:
    """Return True for the Windows-only file-handle races we retry over."""
    if sys.platform != "win32":
        return False
    msg = str(err).lower()
    return (
        "access is denied" in msg
        or "permission denied" in msg
        or "permissiondenied" in msg
        or "os error 5" in msg
        or "(os error 32)" in msg  # The process cannot access the file.
    )


# Schema definition -- kept here because the same field set is consulted by
# writer (build_document -> backend.add) and reader (search.search).
#
# Every text field in this schema uses the ``raw`` tokenizer: Tantivy stores
# each entry verbatim as a single term, with no splitting on punctuation/case
# and no length filter. The indexer is responsible for producing the right
# tokens for each field -- including domain-appropriate splits like dotted
# namespaces (``Acme.Billing.Service`` -> three entries) and per-directory
# path components (``services/billing/Foo.cs`` -> ``services``, ``billing``,
# ``Foo``, ``cs``, ``Foo.cs``). Putting all splitting in the indexer means
# every language can decide its own rules and no token is ever silently
# dropped because Tantivy's SimpleTokenizer didn't like it.
SEARCHABLE_FIELDS: tuple[str, ...] = (
    "namespace",
    "tokens",
    "class_names", "method_names",
    "member_sig_tokens",
    "base_types", "field_types", "local_types",
    "param_types", "return_types", "cast_types",
    "type_refs", "call_sites", "qualified_calls", "member_accesses",
    "attr_names", "imports",
    "type_visibilities", "member_visibilities",
    "path_tokens",
)

# Multi-value fields (added several times per document).
MULTI_VALUE_FIELDS: frozenset[str] = frozenset({
    "class_names", "method_names",
    "member_sig_tokens",
    "base_types", "field_types", "local_types",
    "param_types", "return_types", "cast_types",
    "type_refs", "call_sites", "qualified_calls", "member_accesses",
    "attr_names", "imports",
    "type_visibilities", "member_visibilities",
    "namespace",
    "tokens",
    "path_tokens",
    "path_segments",
})

# Fields stored verbatim and used as exact-match filter terms.
RAW_FIELDS: frozenset[str] = frozenset({
    "id", "relative_path", "extension", "language", "path_segments",
})

# Fields kept ``stored=True`` so values can be retrieved at search time:
# the document id, what file matched, when it was indexed (verifier diff),
# and the basename + ext/language for display & filtering. Every other text
# field on the schema is ``stored=False`` -- indexed for search but not
# retrievable. The AST stage is what actually produces line-level output,
# so the index doesn't need to carry display-only payload.
STORED_FIELDS: frozenset[str] = frozenset({
    "id", "relative_path", "filename", "extension", "language",
    "path_segments", "mtime",
})


def build_schema() -> tantivy.Schema:
    sb = tantivy.SchemaBuilder()
    # -- Stored fields: retrievable at search time -------------------------
    sb.add_text_field("id",            stored=True, tokenizer_name="raw")
    sb.add_text_field("relative_path", stored=True, tokenizer_name="raw")
    sb.add_text_field("extension",     stored=True, tokenizer_name="raw")
    sb.add_text_field("language",      stored=True, tokenizer_name="raw")
    sb.add_text_field("path_segments", stored=True, tokenizer_name="raw")
    sb.add_text_field("filename",      stored=True, tokenizer_name="raw")

    # -- Search-only multi-value raw fields (stored=False) ----------------
    # Indexed for query_by matching, not retrievable from the document.
    # The AST post-filter produces the line-level output; the index only
    # needs to know whether each file CONTAINS a given identifier so it can
    # pre-filter the candidate set. Storing every identifier list per doc
    # would bloat the index for no real benefit.
    for f in (
        "namespace",
        "class_names", "method_names",
        "member_sig_tokens",   # every identifier inside a sig
        "base_types", "field_types", "local_types",
        "param_types", "return_types", "cast_types",
        "type_refs", "call_sites",
        "qualified_calls",     # ``Type.Method`` forms (static + resolved-receiver)
        "member_accesses",
        "attr_names", "imports",
        # Canonical access modifiers captured per declaration. Two parallel
        # fields so a query for "files that have at least one public
        # member" doesn't catch files that only have public *types*.
        "type_visibilities",
        "member_visibilities",
        "tokens",       # deduped bag of every identifier in the file
        "path_tokens",  # per-directory + filename parts
    ):
        sb.add_text_field(f, stored=False, tokenizer_name="raw")

    # File modification time -- fast field so export_id_mtime() stays cheap.
    sb.add_unsigned_field("mtime", stored=True, fast=True)
    return sb.build()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class Backend:
    """One Tantivy index, with optional writer."""

    HEAP_BYTES = 50_000_000  # 50 MB -- default index buffer

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
            self._index: tantivy.Index | None = tantivy.Index(build_schema(), path=self.path)
        else:
            self._index = tantivy.Index.open(self.path)

        # tantivy's default reader policy (``OnCommitWithDelay``) spawns a
        # background watcher thread that polls the directory every 50 ms.
        # For read-only backends that thread:
        #   * is wasted work -- short-lived test backends don't need auto-reload
        #   * keeps an ``Arc<Index>`` reference until it joins on Drop, which
        #     on Windows delays mmap release long enough that the next
        #     ``drop()`` hits ``PermissionDenied`` and silently corrupts state
        # ``manual`` policy creates a reader with no thread. We already call
        # ``reload()`` explicitly before each read, so we lose nothing.
        if not write:
            self._index.config_reader(reload_policy="manual")

        self._schema: tantivy.Schema | None = self._index.schema
        if write:
            self._writer = self._index.writer(heap_size=self.HEAP_BYTES)

    # -- lifecycle ------------------------------------------------------------

    def close(self, quick: bool = False) -> None:
        """Release the writer and the underlying index handle.

        quick=True skips the merge-thread wait so the caller can exit fast.
        Any uncommitted buffered work is dropped; the verifier re-indexes on
        the next startup.

        On Windows, ``tantivy.Index`` keeps memory-mapped file handles even for
        read-only access. Dropping the writer alone is not enough -- the mmap
        survives, ``drop()`` can't ``rmtree`` the locked files, and the next
        writer fails on commit with ``PermissionDenied``. Clearing the index
        reference and forcing a GC pass releases the Rust-side handles so a
        following ``drop()`` actually wipes the directory.
        """
        if self._writer is not None:
            if not quick and self._dirty:
                # Commit any pending work; failures are logged inside commit().
                try:
                    self.commit()
                except Exception:
                    # Already logged by commit(); close() must not raise.
                    pass
            with self._lock:
                if self._writer is not None:
                    if not quick:
                        try:
                            self._writer.wait_merging_threads()
                        except Exception:
                            # Best-effort cleanup; closing the writer below is what matters.
                            pass
                    self._writer = None
        self._index = None
        self._schema = None
        gc.collect()

    def __enter__(self) -> "Backend":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _require_index(self) -> tantivy.Index:
        if self._index is None:
            raise RuntimeError(f"Backend at {self.path} has been closed")
        return self._index

    @property
    def schema(self) -> tantivy.Schema:
        if self._schema is None:
            raise RuntimeError(f"Backend at {self.path} has been closed")
        return self._schema

    def num_documents(self) -> int:
        idx = self._require_index()
        idx.reload()
        return idx.searcher().num_docs

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
        Resets ``has_pending`` regardless of outcome -- committed items move
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
                self._require_index().reload()
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
        if self._writer is not None:
            try:
                self._writer.wait_merging_threads()
            except Exception:
                # The writer is already in an undefined state (we're recovering
                # from a commit failure); skip the wait and rebuild from disk.
                pass
        self._writer = None
        gc.collect()
        # Reload the index handle so the new writer sees the post-rollback state.
        # If no commit has ever succeeded, meta.json may not exist yet -- fall
        # back to creating a fresh index against the same schema.
        if _has_index_files(self.path):
            self._index = tantivy.Index.open(self.path)
        else:
            self._index = tantivy.Index(build_schema(), path=self.path)
        self._schema = self._index.schema
        self._writer = self._index.writer(heap_size=self.HEAP_BYTES)

    # -- writes ---------------------------------------------------------------

    def upsert_many(self, docs: list[dict]) -> tuple[int, int]:
        """Add a batch of documents and commit. Convenience wrapper around
        ``add`` + ``commit`` used by tests and the synchronous indexer.

        Returns (n_ok, n_failed). On commit failure all docs are reported as
        failed (Tantivy's commit is atomic -- partial application isn't a
        thing here). Transient Windows IO errors are retried with backoff:
        the writer's in-memory buffer is discarded on a failed commit so
        we re-run the entire add + commit sequence after a brief settle
        delay.
        """
        if not docs:
            return 0, 0

        last_err: BaseException | None = None
        for attempt in range(_COMMIT_RETRY_ATTEMPTS):
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
                return n_added, len(docs) - n_added
            except Exception as commit_err:
                last_err = commit_err
                if (attempt + 1 >= _COMMIT_RETRY_ATTEMPTS
                        or not _is_transient_windows_io_error(commit_err)):
                    break
                delay = _COMMIT_RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[backend] commit attempt {attempt + 1}/{_COMMIT_RETRY_ATTEMPTS} "
                    f"hit a transient Windows IO error in {self.path}; "
                    f"retrying after {delay:.2f}s",
                    flush=True,
                )
                # commit()'s error handler already rolled back and reopened
                # the writer, so we just need to settle and retry.
                gc.collect()
                time.sleep(delay)
        # Out of retries or non-transient error: caller treats as full failure.
        if last_err is not None:
            print(
                f"[backend] upsert_many giving up on {len(docs)} docs in "
                f"{self.path}: {type(last_err).__name__}: {last_err}",
                flush=True,
            )
        return 0, len(docs)

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
        self._require_index().reload()

    # -- reads ----------------------------------------------------------------

    def searcher(self) -> tantivy.Searcher:
        idx = self._require_index()
        idx.reload()
        return idx.searcher()

    def export_id_mtime(self) -> dict[str, int]:
        """Return {doc_id: mtime} for every document. Used by the verifier."""
        searcher = self.searcher()
        n = searcher.num_docs
        if n == 0:
            return {}
        result = searcher.search(tantivy.Query.all_query(), limit=n)
        out: dict[str, int] = {}
        for _, addr in result.hits:
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
    """Remove the index directory entirely. Used by --resethard.

    On Windows, ``tantivy.Index`` keeps mmap handles even after a Backend's
    Python-level ``close()``. If a callers's last reference hasn't been GC'd
    yet, ``rmtree`` will silently skip locked files and the next ``Backend``
    opens stale residue (which then fails on commit with PermissionDenied).
    Force a GC pass first; on Windows, retry briefly because the OS sometimes
    holds files open for a moment after handles are released. Raise on
    persistent failure so callers learn about the problem instead of silently
    indexing into a half-wiped directory.
    """
    p = Path(path)
    if not p.exists():
        return
    gc.collect()
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(p)
            return
        except OSError as e:
            last_err = e
            if not p.exists():
                return
            gc.collect()
            import time as _time
            _time.sleep(0.1 * (attempt + 1))
    raise RuntimeError(f"drop({path}) failed after retries: {last_err}")


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
# Path resolution -- where the index for a given root lives on disk.
# ---------------------------------------------------------------------------

def index_dir_for(repo_root: str | Path, collection: str) -> str:
    """Return <repo_root>/.tantivy/<collection>/ (created on demand)."""
    base = Path(repo_root) / ".tantivy" / collection
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def sanitize_collection_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())
