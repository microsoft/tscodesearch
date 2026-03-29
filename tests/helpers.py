"""
Shared test helpers and source constants for the codesearch test suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from indexserver.config import HOST, PORT, API_KEY


def _server_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=3) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def _assert_server_ok() -> None:
    """Raise RuntimeError if Typesense is not running. Call from setUpClass."""
    import time
    for _ in range(5):
        if _server_ok():
            return
        time.sleep(1)
    raise RuntimeError("Typesense is not running — start with: ts start")


def _search(collection: str, q: str,
            query_by: str = "filename,class_names,method_names,tokens",
            per_page: int = 10) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": q, "query_by": query_by,
        "per_page": per_page, "num_typos": 0,
    })
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{params}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=5) as r:
        return [h["document"] for h in json.loads(r.read()).get("hits", [])]


def _collection_info(collection: str) -> dict | None:
    url = f"http://{HOST}:{PORT}/collections/{collection}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _delete_collection(collection: str) -> None:
    url = f"http://{HOST}:{PORT}/collections/{collection}"
    req = urllib.request.Request(url, method="DELETE",
                                  headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


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


class _MockDocuments:
    def __init__(self):
        self.upserted: list[dict] = []
        self.deleted: list[str] = []
        self._stored: dict[str, dict] = {}  # doc_id → stored doc (for mtime check)

    def import_(self, docs, params):
        for doc in docs:
            self._stored[doc["id"]] = doc
        self.upserted.extend(docs)
        return [{"success": True}] * len(docs)

    def __getitem__(self, doc_id: str):
        parent = self
        class _Doc:
            def delete(self_):
                parent.deleted.append(doc_id)
                parent._stored.pop(doc_id, None)
            def retrieve(self_):
                if doc_id in parent._stored:
                    return parent._stored[doc_id]
                raise Exception(f"404 not found: {doc_id}")
        return _Doc()


class _MockCollection:
    def __init__(self):
        self.documents = _MockDocuments()


class _MockTypesenseClient:
    def __init__(self, collection_name: str = "test_coll"):
        self._colls: dict[str, _MockCollection] = {collection_name: _MockCollection()}

    @property
    def collections(self):
        return self._colls


class _FakeEvent:
    def __init__(self, src_path: str, is_directory: bool = False, dest_path: str = ""):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


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
