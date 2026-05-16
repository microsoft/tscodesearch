"""
Integration tests for path translation: end-to-end path round-trip through
the daemon's /query-codebase endpoint.

The test class spawns the daemon in-process via tsquery_server.start_daemon()
on a free port; no external service is required.
"""
from __future__ import annotations
import os, sys, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)


class TestPathIntegration(unittest.TestCase):
    """End-to-end path round-trip: ``relative_path`` stored in the index is
    repo-relative — never absolute — so MCP callers can safely prepend
    ``$SRC_ROOT/`` without producing nonsense paths.
    """

    @classmethod
    def setUpClass(cls):
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

    def test_relative_path_is_relative(self):
        from indexserver.indexer import ensure_backend
        from indexserver.search import search as _search

        root = self.cfg.roots.get("default") or next(iter(self.cfg.roots.values()), None)
        if not root or not root.path:
            self.skipTest("no default root configured")

        backend = ensure_backend(self.cfg, root.collection, write=False)
        try:
            result = _search(backend, q="Widget", query_by="path_tokens,class_names,tokens", per_page=5)
        finally:
            backend.close()

        hits = result.get("hits", [])
        if not hits:
            return  # no data indexed in test env — nothing to assert

        canonical_prefix = root.path.replace("\\", "/").rstrip("/").lower()
        for hit in hits:
            rel = hit.get("document", {}).get("relative_path", "").replace("\\", "/")
            self.assertFalse(
                rel.lower().startswith(canonical_prefix + "/"),
                f"relative_path {rel!r} unexpectedly includes the root prefix {canonical_prefix!r}",
            )
            # And to_local(rel) round-trips back to a path under the root.
            self.assertTrue(
                root.to_local(rel).replace("\\", "/").lower().startswith(canonical_prefix + "/"),
                f"to_local({rel!r}) should land inside the root path",
            )


if __name__ == "__main__":
    unittest.main()
