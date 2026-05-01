"""
Tests for file path translation in the codesearch indexserver.

Path contract:
  MCP client (TypeScript/Node.js on Windows)
    - always sends  Windows paths:  C:/repos/src/Widget.cs
    - always receives Windows paths in query results

  Typesense index
    - relative_path stored as a bare relative path:  sub/Widget.cs

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
  TestRootPathConversion     — Root.to_local() and Root.to_external()
  TestToWindowsPathLogic     — MCP-side toWindowsPath() logic (Python port)
  TestPathIntegration        — end-to-end path round-trip (requires server)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def _parse(self, raw: dict, platform: str = "linux", wsl: bool = False):
        from indexserver import config as _cfg
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=wsl):
            mock_sys.platform = platform
            return _cfg._parse_roots(raw)

    def test_both_paths_explicit_uses_local_path(self):
        roots = self._parse({
            "default": {"local_path": "/source/default", "external_path": "C:/repos/src"}
        })
        self.assertEqual(roots["default"].local_path, "/source/default")
        self.assertEqual(roots["default"].external_path, "C:/repos/src")

    def test_only_external_path_wsl_derives_local(self):
        roots = self._parse(
            {"default": {"external_path": "C:/repos/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(roots["default"].local_path, "/mnt/c/repos/src")
        self.assertEqual(roots["default"].external_path, "C:/repos/src")

    def test_only_external_path_docker_falls_back_to_windows(self):
        # Docker mode: cannot auto-derive — must be explicit in config.json
        roots = self._parse(
            {"default": {"external_path": "C:/repos/src"}},
            platform="linux", wsl=False,
        )
        self.assertEqual(roots["default"].local_path, "C:/repos/src")
        self.assertEqual(roots["default"].external_path, "C:/repos/src")

    def test_only_local_path_no_external_path(self):
        roots = self._parse({
            "default": {"local_path": "/source/default"}
        })
        self.assertEqual(roots["default"].local_path, "/source/default")
        self.assertEqual(roots["default"].external_path, "")

    def test_trailing_slashes_stripped(self):
        roots = self._parse({
            "default": {"local_path": "/source/default/", "external_path": "C:/repos/src/"}
        })
        self.assertEqual(roots["default"].local_path, "/source/default")
        self.assertEqual(roots["default"].external_path, "C:/repos/src")

    def test_multiple_roots(self):
        roots = self._parse({
            "app":  {"local_path": "/source/app",  "external_path": "C:/app/src"},
            "libs": {"local_path": "/source/libs", "external_path": "D:/libs/src"},
        })
        self.assertEqual(roots["app"].local_path,  "/source/app")
        self.assertEqual(roots["libs"].local_path, "/source/libs")
        self.assertEqual(roots["libs"].external_path, "D:/libs/src")

    def test_local_path_not_overridden_when_both_set(self):
        # explicit local_path must always win over auto-derived
        roots = self._parse(
            {"default": {"local_path": "/custom/local", "external_path": "C:/repos/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(roots["default"].local_path, "/custom/local")

    def test_wsl_drive_letter_lowercase_in_mnt(self):
        roots = self._parse(
            {"default": {"external_path": "Q:/myproject/src"}},
            platform="linux", wsl=True,
        )
        self.assertEqual(roots["default"].local_path, "/mnt/q/myproject/src")

    def test_extensions_parsed_and_normalized(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({
            "default": {"local_path": "/source/default", "extensions": [".CS", "py", ".Ts"]}
        })
        self.assertEqual(roots["default"].extensions, frozenset({".cs", ".py", ".ts"}))
        self.assertNotEqual(roots["default"].extensions, INCLUDE_EXTENSIONS)

    def test_extensions_absent_defaults_to_include_extensions(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({
            "default": {"local_path": "/source/default"}
        })
        self.assertIs(roots["default"].extensions, INCLUDE_EXTENSIONS)

    def test_extensions_empty_list_defaults_to_include_extensions(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({
            "default": {"local_path": "/source/default", "extensions": []}
        })
        self.assertIs(roots["default"].extensions, INCLUDE_EXTENSIONS)


# ── _resolve_query_paths path resolution ─────────────────────────────────────

class TestRunQueryPathResolution(unittest.TestCase):
    """_resolve_query_paths() translates and validates paths from client requests."""

    def _resolve(self, file_path: str, external_paths: dict, roots: dict) -> list[Path]:
        """Call _resolve_query_paths with patched ALL_ROOTS built from external_paths + roots dicts."""
        import indexserver.api as _api
        from indexserver.config import Root, collection_for_root, INCLUDE_EXTENSIONS
        patched = {
            name: Root(
                name=name,
                local_path=roots.get(name, ""),
                external_path=external_paths.get(name, ""),
                collection=collection_for_root(name),
                extensions=INCLUDE_EXTENSIONS,
            )
            for name in set(list(roots) + list(external_paths))
        }
        with patch.dict("indexserver.api.ALL_ROOTS", patched, clear=True):
            return _api._resolve_query_paths([file_path])

    def test_docker_windows_to_container_path(self):
        """C:/repos/src/Widget.cs → /source/default/Widget.cs in Docker."""
        result = self._resolve(
            "C:/repos/src/Widget.cs",
            external_paths={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        self.assertEqual(result, [Path("/source/default/Widget.cs")])

    def test_docker_subdir_path(self):
        result = self._resolve(
            "C:/repos/src/services/Widget.cs",
            external_paths={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        self.assertEqual(result, [Path("/source/default/services/Widget.cs")])

    def test_wsl_windows_to_mnt_path(self):
        """C:/repos/src/Widget.cs → /mnt/c/repos/src/Widget.cs in WSL."""
        result = self._resolve(
            "C:/repos/src/Widget.cs",
            external_paths={"default": "C:/repos/src"},
            roots={"default": "/mnt/c/repos/src"},
        )
        self.assertEqual(result, [Path("/mnt/c/repos/src/Widget.cs")])

    def test_case_insensitive_external_path_matching(self):
        """external_path matching is case-insensitive (Windows FS)."""
        result = self._resolve(
            "C:/REPOS/SRC/Widget.cs",
            external_paths={"default": "C:/repos/src"},
            roots={"default": "/source/default"},
        )
        self.assertEqual(len(result), 1)
        self.assertIn("Widget.cs", result[0].name)

    def test_path_outside_root_raises(self):
        """A path not under any configured root raises ValueError."""
        with self.assertRaises(ValueError):
            self._resolve(
                "/etc/passwd",
                external_paths={},
                roots={"default": "/source/default"},
            )

    def test_result_file_key_is_native_path(self):
        """_run_query uses the native (server-local) path as the 'file' key."""
        import indexserver.api as _api

        src = b"namespace Widget { public class Widget { } }"
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False) as f:
            f.write(src)
            tmp_path = f.name

        try:
            native_path = Path(tmp_path)
            results = _api._run_query("classes", "", [native_path])
            for r in results:
                self.assertEqual(r["file"], str(native_path.resolve()))
        finally:
            os.unlink(tmp_path)


# ── Root.to_local / Root.to_external ─────────────────────────────────────────

class TestRootPathConversion(unittest.TestCase):
    """Root.to_local() converts stored relative path → local abs path.
    Root.to_external() converts stored relative path → external abs path.
    """

    def _root(self, local_path, external_path=""):
        from indexserver.config import Root, collection_for_root, INCLUDE_EXTENSIONS
        return Root(
            name="default",
            local_path=local_path,
            external_path=external_path,
            collection=collection_for_root("default"),
            extensions=INCLUDE_EXTENSIONS,
        )

    def test_to_local_wsl(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_local("sub/Widget.cs"), "/mnt/c/repos/src/sub/Widget.cs")

    def test_to_local_docker(self):
        root = self._root("/source/default", "")
        self.assertEqual(root.to_local("sub/Widget.cs"), "/source/default/sub/Widget.cs")

    def test_to_local_strips_leading_slash(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_local("/sub/Widget.cs"), "/mnt/c/repos/src/sub/Widget.cs")

    def test_to_local_backslashes_normalised(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_local("sub\\Widget.cs"), "/mnt/c/repos/src/sub/Widget.cs")

    def test_to_external_with_external_path(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_external("sub/Widget.cs"), "C:/repos/src/sub/Widget.cs")

    def test_to_external_no_external_path_falls_back_to_local(self):
        root = self._root("/source/default", "")
        self.assertEqual(root.to_external("sub/Widget.cs"), "/source/default/sub/Widget.cs")

    def test_to_external_strips_leading_slash(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_external("/sub/Widget.cs"), "C:/repos/src/sub/Widget.cs")

    def test_to_local_root_level_file(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_local("Widget.cs"), "/mnt/c/repos/src/Widget.cs")

    def test_to_external_root_level_file(self):
        root = self._root("/mnt/c/repos/src", "C:/repos/src")
        self.assertEqual(root.to_external("Widget.cs"), "C:/repos/src/Widget.cs")


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
      - /query-codebase returns relative_path as the external (Windows) absolute path
    """

    @classmethod
    def setUpClass(cls):
        _assert_api_ok()
        from indexserver.config import HOST, API_PORT, API_KEY, ALL_ROOTS
        cls.host      = HOST
        cls.api_port  = API_PORT
        cls.api_key   = API_KEY
        cls.all_roots = ALL_ROOTS

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
        root = self.all_roots.get(root_name)
        wp = (root.external_path or root.local_path) if root else ""
        return f"{wp.rstrip('/')}/{rel}"

    def _local_path_for(self, rel: str, root_name: str = "default") -> str:
        from indexserver.config import to_native_path
        root = self.all_roots.get(root_name)
        lp = root.local_path if root else ""
        return to_native_path(f"{lp.rstrip('/')}/{rel}")

    def test_query_accepts_external_path_and_returns_it(self):
        """/query: Windows path sent → same Windows path in result 'file' key."""
        root_name = "default"
        root = self.all_roots.get(root_name)
        if not root or not root.local_path:
            self.skipTest("no root configured")

        # Find any .cs file in the local root
        cs_file = None
        from indexserver.config import to_native_path
        native_root = to_native_path(root.local_path)
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
        if root.external_path:
            # e.g. /mnt/c/repos/src/sub/Foo.cs → C:/repos/src/sub/Foo.cs
            lp = to_native_path(root.local_path)
            rel = cs_file[len(lp.rstrip("/")) + 1:]
            external_path = root.external_path.rstrip("/") + "/" + rel
        else:
            external_path = cs_file

        result = self._post("/query", {"mode": "methods", "pattern": "", "files": [external_path]})
        self.assertIn("results", result)
        for r in result["results"]:
            # Each result must echo back the original Windows path
            self.assertEqual(r["file"], external_path,
                             "result 'file' must match the Windows path sent in the request")

    def test_query_codebase_relative_path_is_external_path(self):
        """/query-codebase: relative_path in results must be the external (Windows) path."""
        root = self.all_roots.get("default")
        if not root or not root.external_path:
            self.skipTest("no external_path configured for default root")

        result = self._post("/query-codebase", {
            "mode": "declarations", "pattern": "Widget",
            "root": "default", "limit": 5,
        })
        # Don't require matches — just check any hits that come back
        hits = result.get("hits", [])
        if not hits:
            return  # no data indexed in test env — nothing to assert

        external_prefix = root.external_path.replace("\\", "/").rstrip("/")
        for hit in hits:
            rel = hit.get("document", {}).get("relative_path", "")
            self.assertTrue(
                rel.lower().startswith(external_prefix.lower() + "/"),
                f"relative_path {rel!r} should be external path starting with {external_prefix!r}",
            )


if __name__ == "__main__":
    unittest.main()
