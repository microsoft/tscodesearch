"""
Integration tests for path translation: end-to-end path round-trip.

TestPathIntegration — requires the indexserver to be running (ts start).
"""
from __future__ import annotations
import os, sys, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _api_ok() -> bool:
    import urllib.request
    from indexserver.config import load_config as _load_config
    _cfg = _load_config()
    try:
        req = urllib.request.Request(
            f"http://{_cfg.host}:{_cfg.api_port}/health",
            headers={"X-TYPESENSE-API-KEY": _cfg.api_key},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return __import__("json").loads(r.read()).get("ok", False)
    except Exception:
        return False


def _assert_api_ok() -> None:
    if not _api_ok():
        raise unittest.SkipTest("indexserver is not running — start with: ts start")


class TestPathIntegration(unittest.TestCase):
    """End-to-end path round-trip tests: MCP Windows path → server → results.

    Requires the indexserver to be running (ts start).
    These tests verify the contract that:
      - /query accepts the canonical config path and returns it unchanged in results
      - /query-codebase returns relative_path as the canonical absolute path
    """

    @classmethod
    def setUpClass(cls):
        _assert_api_ok()
        from indexserver.config import load_config as _load_config
        _cfg = _load_config()
        cls.host      = _cfg.host
        cls.api_port  = _cfg.api_port
        cls.api_key   = _cfg.api_key
        cls.all_roots = _cfg.roots

    def _post(self, endpoint: str, body: dict) -> dict:
        import json, urllib.request
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://{self.host}:{self.api_port}{endpoint}",
            data=data,
            headers={
                "X-TYPESENSE-API-KEY": self.api_key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def test_query_accepts_canonical_path_and_returns_it(self):
        """/query: canonical config path sent → same path in result 'file' key."""
        root_name = "default"
        root = self.all_roots.get(root_name)
        if not root:
            self.skipTest("no root configured")

        # Find any .cs file in the native root
        cs_file = None
        native_root = root.native_path
        for dirpath, _, files in os.walk(native_root):
            for fname in files:
                if fname.endswith(".cs"):
                    cs_file = os.path.join(dirpath, fname).replace("\\", "/")
                    break
            if cs_file:
                break

        if not cs_file:
            self.skipTest("no .cs files found in root")

        # Convert native path → canonical (config) path for the request
        rel = cs_file[len(native_root.rstrip("/")) + 1:]
        canonical_path = root.to_external(rel)

        result = self._post("/query", {"mode": "methods", "pattern": "", "files": [canonical_path]})
        self.assertIn("results", result)
        for r in result["results"]:
            # Each result must echo back the canonical path sent in the request
            self.assertEqual(r["file"], canonical_path,
                             "result 'file' must match the canonical path sent in the request")

    def test_query_codebase_relative_path_is_canonical_path(self):
        """/query-codebase: relative_path in results must be the canonical (config) path."""
        root = self.all_roots.get("default")
        if not root or not root.path:
            self.skipTest("no default root configured")

        result = self._post("/query-codebase", {
            "mode": "declarations", "pattern": "Widget",
            "root": "default", "limit": 5,
        })
        # Don't require matches — just check any hits that come back
        hits = result.get("hits", [])
        if not hits:
            return  # no data indexed in test env — nothing to assert

        canonical_prefix = root.path.replace("\\", "/").rstrip("/")
        for hit in hits:
            rel = hit.get("document", {}).get("relative_path", "")
            self.assertTrue(
                rel.lower().startswith(canonical_prefix.lower() + "/"),
                f"relative_path {rel!r} should start with canonical path {canonical_prefix!r}",
            )


if __name__ == "__main__":
    unittest.main()
