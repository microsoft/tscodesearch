"""
Shared test helpers and source constants for the codesearch test suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _assert_server_ok() -> None:
    """No-op: Tantivy is in-process, no external server to reach."""
    return None


def _search(collection: str, q: str,
            query_by: str = "path_tokens,class_names,method_names,tokens",
            per_page: int = 10) -> list[dict]:
    """Run a backend search and return the documents from each hit."""
    from indexserver.config import load_config as _load_config
    from indexserver.indexer import ensure_backend
    from indexserver.search import search as _backend_search
    cfg = _load_config()
    with ensure_backend(cfg, collection, write=False) as backend:
        result = _backend_search(
            backend, q=q, query_by=query_by, per_page=per_page, num_typos=0,
        )
    return [h["document"] for h in result.get("hits", [])]


def _collection_info(collection: str) -> dict | None:
    """Return info for an existing Tantivy index, or None if it has not been created."""
    import os
    from indexserver.config import load_config as _load_config, index_root
    from indexserver.backend import Backend
    cfg = _load_config()
    root = next((r for r in cfg.roots.values() if r.collection == collection), None)
    index_dir = root.index_dir if root else str(index_root() / collection)
    # Only consider the collection "to exist" if there is a meta.json on disk.
    if not os.path.exists(os.path.join(index_dir, "meta.json")):
        return None
    try:
        with Backend(index_dir, write=False, create=False) as backend:
            return {"num_documents": backend.num_documents()}
    except Exception:
        return None


def _delete_collection(collection: str, timeout: float = 10.0) -> None:
    """Wipe a Tantivy collection's on-disk directory.

    Tests intentionally block here until the directory is verifiably gone — a
    later test that reuses the same path needs to start clean. On Windows,
    even after ``Backend.close()`` clears the index and ``gc.collect()`` runs,
    the OS may briefly hold a mmap'd file. ``drop()`` already retries; this
    wrapper retries the whole drop a few times within ``timeout`` and
    confirms ``os.path.exists`` is ``False`` before returning. Raises on
    persistent failure so the next test fails early with a clear message
    instead of indexing into a half-wiped directory.
    """
    import gc as _gc
    import time as _time
    from indexserver.config import load_config as _load_config, index_root
    from indexserver.backend import drop
    cfg = _load_config()
    root = next((r for r in cfg.roots.values() if r.collection == collection), None)
    index_dir = root.index_dir if root else str(index_root() / collection)

    deadline = _time.time() + timeout
    last_err: Exception | None = None
    while _time.time() < deadline:
        try:
            drop(index_dir)
        except Exception as e:
            last_err = e
        if not os.path.exists(index_dir):
            return
        _gc.collect()
        _time.sleep(0.2)
    raise RuntimeError(
        f"_delete_collection({collection!r}) still sees {index_dir!r} after "
        f"{timeout}s. Last drop error: {last_err}"
    )


def _make_git_repo(files: dict) -> str:
    tmpdir = tempfile.mkdtemp(prefix="ts_idx_test_")
    for rel, content in files.items():
        full = os.path.join(tmpdir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    subprocess.run(["git", "-C", tmpdir, "init", "-q"], check=True)
    subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
    return tmpdir


# ---------------------------------------------------------------------------
# Per-test Tantivy backend on a tempdir
# ---------------------------------------------------------------------------

def make_test_backend():
    """Open a real Tantivy ``Backend`` for one unit test.

    Returns ``(backend, cleanup)``. Call ``cleanup()`` in ``tearDown`` to
    close the backend and remove the on-disk index. Cheap enough (~50 ms
    per setup) that per-test isolation is fine.
    """
    import shutil
    from indexserver.backend import Backend
    path = tempfile.mkdtemp(prefix="ts_backend_test_")
    backend = Backend(path, write=True)

    def cleanup():
        backend.close(quick=True)
        shutil.rmtree(path, ignore_errors=True)

    return backend, cleanup


class _FakeEvent:
    def __init__(self, src_path: str, is_directory: bool = False, dest_path: str = ""):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


# ---------------------------------------------------------------------------
# Source fixtures (kept identical so unit tests keep their assertions)
# ---------------------------------------------------------------------------

_FOO_CS = """\
using System;
namespace TestNs {
    [Serializable]
    public class Foo : IDisposable, IComparable {
        public string Name { get; set; }
        public void Dispose() { }
        public int CompareTo(object obj) { return 0; }
        public void DoWork(string input) { }
    }
}
"""

_BAR_CS = """\
namespace TestNs {
    public class Bar : Foo {
        private Foo _foo;
        public Bar(Foo foo) { _foo = foo; }
        public void Process() { _foo.DoWork("hello"); }
    }
}
"""

_BLOBSTORE_CS = """\
using System.Threading.Tasks;
namespace Storage {
    public interface IBlobStore {
        Task WriteAsync(string key, byte[] data);
        Task<byte[]> ReadAsync(string key);
    }
    public class BlobStore : IBlobStore {
        public async Task WriteAsync(string key, byte[] data) { }
        public async Task<byte[]> ReadAsync(string key) { return new byte[0]; }
    }
}
"""

_QUALIFIED_CS = """\
namespace MyApp {
    [My.Auth.AuthorizeAttribute]
    public class Widget : Acme.IBlobStore, Generic.IComparable<Widget> {
        private Acme.IBlobStore _store;
        public Acme.IBlobStore Store { get; set; }
        public string Process(Acme.IBlobStore store) { return ""; }
    }
}
"""

_GENERIC_WRAPPER_CS = """\
using System.Collections.Generic;
using System.Threading.Tasks;
namespace MyApp {
    public class WidgetService {
        private IList<IBlobStore> _stores;
        public IReadOnlyList<IBlobStore> Stores { get; set; }
        public Task<IBlobStore> GetAsync(string key) { return null; }
        public void Register(IList<IBlobStore> stores) { }
    }
}
"""

_FOO_PY = """\
from __future__ import annotations
import os
from typing import Optional

class IFoo:
    def process(self, data: str) -> None:
        pass

class IComparable:
    def compare(self, other) -> int:
        return 0

def dataclass(cls):
    return cls

@dataclass
class Foo(IFoo, IComparable):
    name: str = ""

    def process(self, data: str) -> None:
        print(data)

    def compute(self, value: int) -> Optional[str]:
        return str(value)

def variadic(*args: str, **kwargs: int) -> None:
    pass

def kw_only(name: str, *, debug: bool = False, timeout: int = 30) -> None:
    pass
"""

_BAR_PY = """\
from myapp.foo import Foo

class Bar(Foo):
    def __init__(self, foo: Foo) -> None:
        self._foo = foo

    def run(self) -> None:
        self._foo.process("hello")
"""
