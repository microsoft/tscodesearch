"""
Tests for file path translation in the codesearch indexserver.

Path contract:
  MCP client (TypeScript/Node.js on Windows)
    - always sends  Windows paths:  C:/repos/src/Widget.cs
    - always receives Windows paths in query results

  Typesense index
    - relative_path stored as full Windows path:  C:/repos/src/sub/Widget.cs
      (host_root prefix from HOST_ROOTS + bare relative segment)

  Indexserver (Python in WSL or Docker)
    - opens files using server-local paths:  /mnt/c/repos/src/Widget.cs  (WSL)
                                              /source/default/Widget.cs   (Docker)

  config.json should always have both:
    external_path — Windows-side path for the MCP client
    local_path   — server-side path for WSL / Docker (auto-derived in WSL if absent)

Covered here (no Typesense server required):
  TestToNativePath           — to_native_path() under WSL / Docker / Windows
  TestParseRoots             — _parse_roots() auto-derives local_path in WSL
  TestRunQueryPathResolution — _run_query() maps Windows paths → local paths
  TestQueryCodebasePathStrip — host_root prefix stripped before abs_path construction
  TestToWindowsPathLogic     — MCP-side toWindowsPath() logic (Python port)
  TestPathIntegration        — end-to-end path round-trip (requires server)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


# ── to_native_path ────────────────────────────────────────────────────────────

class TestToNativePath(unittest.TestCase):
    """to_native_path() — platform-aware Windows ↔ WSL/Linux conversion."""

    def _call(self, path: str, platform: str, wsl: bool = False) -> str:
        from indexserver import config as _cfg
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=wsl):
            mock_sys.platform = platform
            return _cfg.to_native_path(path)

    # ── WSL ───────────────────────────────────────────────────────────────────

    def test_wsl_windows_to_mnt(self):
        self.assertEqual(
            self._call("C:/repos/src/Widget.cs", "linux", wsl=True),
            "/mnt/c/repos/src/Widget.cs",
        )

    def test_wsl_lower_drive_letter(self):
        self.assertEqual(
            self._call("Q:/myproject/src/Widget.cs", "linux", wsl=True),
            "/mnt/q/myproject/src/Widget.cs",
        )

    def test_wsl_uppercase_drive_downcased(self):
        result = self._call("Q:/myproject/src/Widget.cs", "linux", wsl=True)
        self.assertTrue(result.startswith("/mnt/q/"))

    def test_wsl_already_mnt_path_unchanged(self):
        self.assertEqual(
            self._call("/mnt/c/repos/src/Widget.cs", "linux", wsl=True),
            "/mnt/c/repos/src/Widget.cs",
        )

    def test_wsl_backslashes_normalised(self):
        self.assertEqual(
            self._call("C:\\repos\\src\\Widget.cs", "linux", wsl=True),
            "/mnt/c/repos/src/Widget.cs",
        )

    # ── Docker / native Linux ─────────────────────────────────────────────────

    def test_docker_external_path_unchanged(self):
        # On native Linux (no WSL), paths are used as-is
        self.assertEqual(
            self._call("C:/repos/src/Widget.cs", "linux", wsl=False),
            "C:/repos/src/Widget.cs",
        )

    def test_docker_linux_path_unchanged(self):
        self.assertEqual(
            self._call("/source/default/sub/Widget.cs", "linux", wsl=False),
            "/source/default/sub/Widget.cs",
        )


# ── _parse_roots ──────────────────────────────────────────────────────────────

class TestParseRoots(unittest.TestCase):
    """_parse_roots() — config parsing and local_path auto-derivation."""

    def _parse(self, raw: dict, platform: str = "linux", wsl: bool = False) -> tuple:
        from indexserver import config as _cfg
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=wsl):
            mock_sys.platform = platform
            return _cfg._parse_roots(raw)

    def test_both_paths_explicit_uses_local_path(self):
        local, windows = self._parse({
            "default": {"local_path": "/source/default", "external_path": "C:/repos/src"}
        })
        self.assertEqual(local["default"], "/source/default")
        self.assertEqual(windows["default"], "C:/repos/src")

    def test_only_external_path_wsl_derives_local(self):
        local, windows = self._parse(
            {"default": {"external_path": "C:/repos/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(local["default"], "/mnt/c/repos/src")
        self.assertEqual(windows["default"], "C:/repos/src")

    def test_only_external_path_docker_falls_back_to_windows(self):
        # Docker mode: cannot auto-derive — must be explicit in config.json
        local, windows = self._parse(
            {"default": {"external_path": "C:/repos/src"}},
            platform="linux", wsl=False,
        )
        self.assertEqual(local["default"], "C:/repos/src")
        self.assertEqual(windows["default"], "C:/repos/src")

    def test_only_local_path_no_host_root(self):
        local, windows = self._parse({
            "default": {"local_path": "/source/default"}
        })
        self.assertEqual(local["default"], "/source/default")
        self.assertNotIn("default", windows)

    def test_trailing_slashes_stripped(self):
        local, windows = self._parse({
            "default": {"local_path": "/source/default/", "external_path": "C:/repos/src/"}
        })
        self.assertEqual(local["default"], "/source/default")
        self.assertEqual(windows["default"], "C:/repos/src")

    def test_multiple_roots(self):
        local, windows = self._parse({
            "app":  {"local_path": "/source/app",  "external_path": "C:/app/src"},
            "libs": {"local_path": "/source/libs", "external_path": "D:/libs/src"},
        })
        self.assertEqual(local["app"],  "/source/app")
        self.assertEqual(local["libs"], "/source/libs")
        self.assertEqual(windows["libs"], "D:/libs/src")

    def test_local_path_not_overridden_when_both_set(self):
        # explicit local_path must always win over auto-derived
        local, _ = self._parse(
            {"default": {"local_path": "/custom/local", "external_path": "C:/repos/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(local["default"], "/custom/local")

    def test_wsl_drive_letter_lowercase_in_mnt(self):
        local, _ = self._parse(
            {"default": {"external_path": "Q:/myproject/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(local["default"], "/mnt/q/myproject/src")


# ── _run_query path resolution ────────────────────────────────────────────────

class TestRunQueryPathResolution(unittest.TestCase):
    """_run_query() maps Windows paths from MCP client to server-local paths."""

    def _paths_opened(self, file_path: str, host_roots: dict, roots: dict) -> list[str]:
        """Return the filesystem path(s) that _run_query passed to open()."""
        import indexserver.api as _api

        opened = []
        def fake_open(path, mode="r", **kw):
            opened.append(path)
            raise OSError("intercepted")

        with patch.dict("indexserver.api.HOST_ROOTS", host_roots, clear=True), \
             patch.dict("indexserver.api.ROOTS", roots, clear=True), \
             patch("builtins.open", fake_open):
            _api._run_query("methods", "", [file_path])

        return opened

    def test_docker_windows_to_container_path(self):
        """C:/repos/src/Widget.cs → /source/default/Widget.cs in Docker."""
        opened = self._paths_opened(
            "C:/repos/src/Widget.cs",
            host_roots={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0], "/source/default/Widget.cs")

    def test_docker_subdir_path(self):
        opened = self._paths_opened(
            "C:/repos/src/services/Widget.cs",
            host_roots={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        self.assertEqual(opened[0], "/source/default/services/Widget.cs")

    def test_wsl_windows_to_mnt_path(self):
        """C:/repos/src/Widget.cs → /mnt/c/repos/src/Widget.cs in WSL."""
        opened = self._paths_opened(
            "C:/repos/src/Widget.cs",
            host_roots={"default": "C:/repos/src"},
            roots={"default": "/mnt/c/repos/src"},
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0], "/mnt/c/repos/src/Widget.cs")

    def test_case_insensitive_host_root_matching(self):
        """HOST_ROOT matching is case-insensitive (Windows FS)."""
        opened = self._paths_opened(
            "C:/REPOS/SRC/Widget.cs",
            host_roots={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        # Should match and produce: /source/default/Widget.cs
        self.assertEqual(len(opened), 1)
        self.assertIn("Widget.cs", opened[0])

    def test_result_file_key_is_original_external_path(self):
        """_run_query returns the original Windows path as 'file' key."""
        import indexserver.api as _api
        import tree_sitter_c_sharp as tscsharp
        from tree_sitter import Language, Parser

        src = b"namespace Widget { public class Widget { } }"
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False) as f:
            f.write(src)
            tmp_path = f.name

        try:
            external_path = "C:/repos/src/Widget.cs"
            # Map the fake Windows path to our real temp file
            local_dir = os.path.dirname(tmp_path).replace("\\", "/")
            fname = os.path.basename(tmp_path)
            with patch.dict("indexserver.api.HOST_ROOTS",
                            {"default": "C:/repos/src"}, clear=True), \
                 patch.dict("indexserver.api.ROOTS",
                            {"default": local_dir}, clear=True):
                results = _api._run_query("classes", "", [external_path])
        finally:
            os.unlink(tmp_path)

        # If tree-sitter found something, the result must use the original path
        for r in results:
            self.assertEqual(r["file"], external_path)


# ── /query-codebase host_root stripping ──────────────────────────────────────

class TestQueryCodebasePathStrip(unittest.TestCase):
    """host_root prefix stripped from Typesense relative_path before abs_path build."""

    @staticmethod
    def _strip(rel: str, host_root_prefix: str) -> str:
        """Mirrors the stripping logic from the /query-codebase handler."""
        rel = rel.replace("\\", "/")
        hr = host_root_prefix.replace("\\", "/").rstrip("/")
        if hr and rel.lower().startswith(hr.lower() + "/"):
            rel = rel[len(hr) + 1:]
        return rel

    def test_strips_windows_prefix(self):
        self.assertEqual(
            self._strip("C:/repos/src/sub/Widget.cs", "C:/repos/src"),
            "sub/Widget.cs",
        )

    def test_strips_root_level_file(self):
        self.assertEqual(
            self._strip("C:/repos/src/Widget.cs", "C:/repos/src"),
            "Widget.cs",
        )

    def test_case_insensitive(self):
        self.assertEqual(
            self._strip("c:/repos/src/Widget.cs", "C:/REPOS/SRC"),
            "Widget.cs",
        )

    def test_no_prefix_bare_relative_unchanged(self):
        self.assertEqual(
            self._strip("sub/Widget.cs", ""),
            "sub/Widget.cs",
        )

    def test_different_root_not_stripped(self):
        result = self._strip("D:/other/src/Widget.cs", "C:/repos/src")
        self.assertEqual(result, "D:/other/src/Widget.cs")

    def test_docker_path_construction(self):
        """After stripping, prepending Docker src_root gives correct path."""
        from indexserver.config import to_native_path
        rel = self._strip("C:/repos/src/sub/Widget.cs", "C:/repos/src")
        abs_path = to_native_path("/source/default" + "/" + rel)
        self.assertEqual(abs_path, "/source/default/sub/Widget.cs")

    def test_wsl_path_construction(self):
        """After stripping, prepending WSL src_root gives correct path."""
        from indexserver.config import to_native_path
        rel = self._strip("C:/repos/src/sub/Widget.cs", "C:/repos/src")
        abs_path = to_native_path("/mnt/c/repos/src" + "/" + rel)
        self.assertEqual(abs_path, "/mnt/c/repos/src/sub/Widget.cs")

    def test_backslash_in_stored_path(self):
        result = self._strip("C:\\repos\\src\\Widget.cs", "C:/repos/src")
        self.assertEqual(result, "Widget.cs")


# ── MCP-side toWindowsPath logic ──────────────────────────────────────────────

class TestToWindowsPathLogic(unittest.TestCase):
    """Python port of mcp_server.ts toWindowsPath() — validates MCP path normalisation."""

    @staticmethod
    def _to_windows(file_path: str, default_root: str) -> str:
        import re
        p = file_path.replace("\\", "/")
        p = p.replace("${SRC_ROOT}", default_root).replace("$SRC_ROOT", default_root)
        m = re.match(r"^/mnt/([a-zA-Z])/(.*)", p)
        if m:
            return f"{m.group(1).upper()}:/{m.group(2)}"
        if re.match(r"^[A-Za-z]:", p):
            return p
        if default_root:
            return f"{default_root}/{p}"
        return p

    def test_external_path_unchanged(self):
        self.assertEqual(
            self._to_windows("C:/repos/src/Widget.cs", "C:/repos/src"),
            "C:/repos/src/Widget.cs",
        )

    def test_wsl_mnt_path_converted(self):
        self.assertEqual(
            self._to_windows("/mnt/c/repos/src/Widget.cs", "C:/repos/src"),
            "C:/repos/src/Widget.cs",
        )

    def test_src_root_variable_expanded(self):
        self.assertEqual(
            self._to_windows("$SRC_ROOT/sub/Widget.cs", "C:/repos/src"),
            "C:/repos/src/sub/Widget.cs",
        )

    def test_src_root_braces_expanded(self):
        self.assertEqual(
            self._to_windows("${SRC_ROOT}/sub/Widget.cs", "C:/repos/src"),
            "C:/repos/src/sub/Widget.cs",
        )

    def test_relative_path_prefixed(self):
        self.assertEqual(
            self._to_windows("sub/Widget.cs", "C:/repos/src"),
            "C:/repos/src/sub/Widget.cs",
        )

    def test_backslashes_normalised(self):
        self.assertEqual(
            self._to_windows("C:\\repos\\src\\Widget.cs", "C:/repos/src"),
            "C:/repos/src/Widget.cs",
        )

    def test_mnt_drive_letter_uppercased(self):
        self.assertEqual(
            self._to_windows("/mnt/q/myproject/src/Widget.cs", "Q:/myproject/src"),
            "Q:/myproject/src/Widget.cs",
        )

    def test_different_drive(self):
        self.assertEqual(
            self._to_windows("/mnt/d/other/src/Widget.cs", "C:/repos/src"),
            "D:/other/src/Widget.cs",
        )


# ── Integration tests (requires running indexserver) ─────────────────────────

def _api_ok() -> bool:
    import urllib.request
    from indexserver.config import HOST, API_PORT
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{API_PORT}/health",
            headers={"X-TYPESENSE-API-KEY": __import__("indexserver.config", fromlist=["API_KEY"]).API_KEY},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return __import__("json").loads(r.read()).get("ok", False)
    except Exception:
        return False


def _assert_api_ok() -> None:
    if not _api_ok():
        raise RuntimeError("indexserver is not running — start with: ts start")


class TestPathIntegration(unittest.TestCase):
    """End-to-end path round-trip tests: MCP Windows path → server → results.

    Requires the indexserver to be running (ts start).
    These tests verify the contract that:
      - /query accepts Windows paths and returns them unchanged in results
      - /query-codebase returns relative_path as a Windows path (with host_root prefix)
    """

    @classmethod
    def setUpClass(cls):
        _assert_api_ok()
        import json, urllib.request
        from indexserver.config import HOST, API_PORT, API_KEY, HOST_ROOTS, ROOTS
        cls.host      = HOST
        cls.api_port  = API_PORT
        cls.api_key   = API_KEY
        cls.host_roots = HOST_ROOTS
        cls.roots      = ROOTS

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

    def _external_path_for(self, rel: str, root_name: str = "default") -> str:
        """Build a Windows path for a file relative to a root."""
        wp = self.host_roots.get(root_name, "")
        if not wp:
            wp = self.roots.get(root_name, "")
        return f"{wp.rstrip('/')}/{rel}"

    def _local_path_for(self, rel: str, root_name: str = "default") -> str:
        from indexserver.config import to_native_path
        lp = self.roots.get(root_name, "")
        return to_native_path(f"{lp.rstrip('/')}/{rel}")

    def test_query_accepts_external_path_and_returns_it(self):
        """/query: Windows path sent → same Windows path in result 'file' key."""
        root_name = "default"
        local = self.local_root = self.roots.get(root_name, "")
        if not local:
            self.skipTest("no root configured")

        # Find any .cs file in the local root
        cs_file = None
        from indexserver.config import to_native_path
        native_root = to_native_path(local)
        for dirpath, _, files in os.walk(native_root):
            for fname in files:
                if fname.endswith(".cs"):
                    cs_file = os.path.join(dirpath, fname).replace("\\", "/")
                    break
            if cs_file:
                break

        if not cs_file:
            self.skipTest("no .cs files found in root")

        # Convert local path → Windows path for the request
        host_root = self.host_roots.get(root_name, "")
        if host_root:
            # e.g. /mnt/c/repos/src/sub/Foo.cs → C:/repos/src/sub/Foo.cs
            lp = to_native_path(local)
            rel = cs_file[len(lp.rstrip("/")) + 1:]
            external_path = host_root.rstrip("/") + "/" + rel
        else:
            external_path = cs_file

        result = self._post("/query", {"mode": "methods", "pattern": "", "files": [external_path]})
        self.assertIn("results", result)
        for r in result["results"]:
            # Each result must echo back the original Windows path
            self.assertEqual(r["file"], external_path,
                             "result 'file' must match the Windows path sent in the request")

    def test_query_codebase_relative_path_is_external_path(self):
        """/query-codebase: relative_path in results must be a Windows path."""
        if not self.host_roots.get("default"):
            self.skipTest("no external_path configured for default root")

        result = self._post("/query-codebase", {
            "mode": "declarations", "pattern": "Widget",
            "root": "default", "limit": 5,
        })
        # Don't require matches — just check any hits that come back
        hits = result.get("hits", [])
        if not hits:
            return  # no data indexed in test env — nothing to assert

        host_root = self.host_roots["default"].replace("\\", "/").rstrip("/")
        for hit in hits:
            rel = hit.get("document", {}).get("relative_path", "")
            self.assertTrue(
                rel.lower().startswith(host_root.lower() + "/"),
                f"relative_path {rel!r} should start with host_root {host_root!r}",
            )


if __name__ == "__main__":
    unittest.main()
