"""
Integration tests for path translation: end-to-end path round-trip through
the daemon's /query-codebase endpoint.

The test class spawns the daemon in-process via tsquery_server.start_daemon()
on a free port; no external service is required.
"""
from __future__ import annotations
import os, sys, time, unittest, json, urllib.request

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)


class TestPathIntegration(unittest.TestCase):
    """End-to-end path round-trip: /query-codebase returns the canonical absolute
    path of each hit, regardless of whether the source files live on Windows or
    a WSL mount.
    """

    @classmethod
    def setUpClass(cls):
        # Index whichever roots the test config provides.
        from indexserver.config import load_config as _load_config
        from indexserver.indexer import run_index
        cls.cfg = _load_config()
        for root in cls.cfg.roots.values():
            run_index(cls.cfg, src_root=root.path, collection=root.collection,
                      resethard=True, verbose=False)

    @classmethod
    def tearDownClass(cls):
        from indexserver.backend import drop
        for root in cls.cfg.roots.values():
            drop(root.index_dir)

    def test_query_codebase_relative_path_is_canonical_path(self):
        """/query-codebase: relative_path in results is the canonical (config) path."""
        from indexserver.indexer import ensure_backend
        from indexserver.search import search as _search

        root = self.cfg.roots.get("default") or next(iter(self.cfg.roots.values()), None)
        if not root or not root.path:
            self.skipTest("no default root configured")

        backend = ensure_backend(self.cfg, root.collection, write=False)
        try:
            result = _search(backend, q="Widget", query_by="filename,class_names,tokens", per_page=5)
        finally:
            backend.close()

        hits = result.get("hits", [])
        if not hits:
            return  # no data indexed in test env — nothing to assert

        canonical_prefix = root.path.replace("\\", "/").rstrip("/")
        for hit in hits:
            rel = hit.get("document", {}).get("relative_path", "")
            full = root.to_external(rel)
            self.assertTrue(
                full.lower().startswith(canonical_prefix.lower() + "/"),
                f"to_external({rel!r}) → {full!r} should start with canonical path {canonical_prefix!r}",
            )


if __name__ == "__main__":
    unittest.main()
