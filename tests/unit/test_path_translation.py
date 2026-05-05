"""
Unit tests for file path translation in the codesearch indexserver.

Path contract:
  MCP client (TypeScript/Node.js on Windows)
    - always sends  Windows paths:  C:/repos/src/Widget.cs
    - always receives Windows paths in query results

  Typesense index
    - relative_path stored as a bare relative path:  sub/Widget.cs

  Indexserver (Python in WSL or Docker)
    - opens files using server-local paths:  /mnt/c/repos/src/Widget.cs  (WSL)
                                              /source/default/Widget.cs   (Docker)

  config.json stores a single canonical ``path`` per root (usually the Windows
  path, e.g. ``C:/repos/src``).  ``to_native_path()`` converts it for file I/O
  when the server runs in WSL.

Covered here (no Typesense server required):
  TestToNativePath           — to_native_path() under WSL / Docker / Windows
  TestParseRoots             — _parse_roots() with new path field and legacy compat
  TestRunQueryPathResolution — _resolve_query_paths() maps paths → local paths
  TestRootPathConversion     — Root.to_local() and Root.to_external()
  TestToWindowsPathLogic     — MCP-side toWindowsPath() logic (Python port)

Integration tests (require running indexserver) are in tests/integration/test_path_translation.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_linux_path_unchanged(self):
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
    """_parse_roots() — config parsing with new path field and legacy compat."""

    def _parse(self, raw: dict, platform: str = "linux", wsl: bool = False):
        from indexserver import config as _cfg
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=wsl):
            mock_sys.platform = platform
            return _cfg._parse_roots(raw)

    # ── new format ────────────────────────────────────────────────────────────

    def test_path_field_used_directly(self):
        roots = self._parse({"default": {"path": "C:/repos/src"}})
        self.assertEqual(roots["default"].path, "C:/repos/src")

    def test_string_value_accepted(self):
        roots = self._parse({"default": "C:/repos/src"})
        self.assertEqual(roots["default"].path, "C:/repos/src")

    def test_trailing_slashes_stripped(self):
        roots = self._parse({"default": {"path": "C:/repos/src/"}})
        self.assertEqual(roots["default"].path, "C:/repos/src")

    def test_multiple_roots(self):
        roots = self._parse({
            "app":  {"path": "C:/app/src"},
            "libs": {"path": "D:/libs/src"},
        })
        self.assertEqual(roots["app"].path,  "C:/app/src")
        self.assertEqual(roots["libs"].path, "D:/libs/src")


    # ── native_path property ─────────────────────────────────────────────────

    def test_native_path_wsl_converts_drive(self):
        from indexserver import config as _cfg
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=True):
            mock_sys.platform = "linux"
            roots = _cfg._parse_roots({"default": {"path": "Q:/myproject/src"}})
            self.assertEqual(roots["default"].native_path, "/mnt/q/myproject/src")

    def test_native_path_docker_unchanged(self):
        roots = self._parse(
            {"default": {"path": "/source/default"}},
            platform="linux", wsl=False,
        )
        self.assertEqual(roots["default"].native_path, "/source/default")

    # ── extensions ───────────────────────────────────────────────────────────

    def test_extensions_parsed_and_normalized(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({
            "default": {"path": "/source/default", "extensions": [".CS", "py", ".Ts"]}
        })
        self.assertEqual(roots["default"].extensions, frozenset({".cs", ".py", ".ts"}))
        self.assertNotEqual(roots["default"].extensions, INCLUDE_EXTENSIONS)

    def test_extensions_absent_defaults_to_include_extensions(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({"default": {"path": "/source/default"}})
        self.assertIs(roots["default"].extensions, INCLUDE_EXTENSIONS)

    def test_extensions_empty_list_defaults_to_include_extensions(self):
        from indexserver.config import INCLUDE_EXTENSIONS
        roots = self._parse({"default": {"path": "/source/default", "extensions": []}})
        self.assertIs(roots["default"].extensions, INCLUDE_EXTENSIONS)


# ── _resolve_query_paths path resolution ─────────────────────────────────────

@unittest.skipIf(sys.platform == "win32", "tests Linux-only path resolution (WSL)")
class TestRunQueryPathResolution(unittest.TestCase):
    """_resolve_query_paths() translates and validates paths from client requests."""

    def _resolve(self, file_path: str, paths: dict) -> list[Path]:
        """Call _resolve_query_paths with patched ALL_ROOTS built from paths dict."""
        import tsquery_server as _api
        from indexserver.config import Root, collection_for_root, INCLUDE_EXTENSIONS
        patched = {
            name: Root(
                name=name,
                path=path,
                collection=collection_for_root(name),
                extensions=INCLUDE_EXTENSIONS,
            )
            for name, path in paths.items()
        }
        with patch.dict("tsquery_server.ALL_ROOTS", patched, clear=True):
            return _api._resolve_query_paths([file_path])

    def test_wsl_windows_to_mnt_path(self):
        """C:/repos/src/Widget.cs → /mnt/c/repos/src/Widget.cs in WSL."""
        result = self._resolve(
            "C:/repos/src/Widget.cs",
            paths={"default": "C:/repos/src"},
        )
        # native_path converts C:/repos/src → /mnt/c/repos/src in WSL
        self.assertEqual(len(result), 1)
        self.assertIn("Widget.cs", result[0].name)

    def test_case_insensitive_path_matching(self):
        """path matching is case-insensitive (Windows FS)."""
        result = self._resolve(
            "C:/REPOS/SRC/Widget.cs",
            paths={"default": "C:/repos/src"},
        )
        self.assertEqual(len(result), 1)
        self.assertIn("Widget.cs", result[0].name)

    def test_path_outside_root_raises(self):
        """A path not under any configured root raises ValueError."""
        with self.assertRaises(ValueError):
            self._resolve(
                "/etc/passwd",
                paths={"default": "/source/default"},
            )

    def test_result_file_key_is_native_path(self):
        """_run_query uses the native (server-local) path as the 'file' key."""
        import tsquery_server as _api

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
    """Root.to_local() converts relative path → native abs path via to_native_path.
    Root.to_external() converts relative path → canonical config path.
    """

    def _root(self, path: str):
        from indexserver.config import Root, collection_for_root, INCLUDE_EXTENSIONS
        return Root(
            name="default",
            path=path,
            collection=collection_for_root("default"),
            extensions=INCLUDE_EXTENSIONS,
        )

    def _root_with_mock(self, path: str, native: str):
        """Return a root whose native_path is forced to `native` (for platform-independent tests)."""
        root = self._root(path)
        # Patch to_native_path so tests aren't affected by the host platform.
        from indexserver import config as _cfg
        original = _cfg.to_native_path
        _cfg.to_native_path = lambda p: native if p == path else original(p)
        return root, lambda: setattr(_cfg, "to_native_path", original)

    def test_to_external_returns_config_path(self):
        root = self._root("C:/repos/src")
        self.assertEqual(root.to_external("sub/Widget.cs"), "C:/repos/src/sub/Widget.cs")

    def test_to_external_strips_leading_slash(self):
        root = self._root("C:/repos/src")
        self.assertEqual(root.to_external("/sub/Widget.cs"), "C:/repos/src/sub/Widget.cs")

    def test_to_external_root_level_file(self):
        root = self._root("C:/repos/src")
        self.assertEqual(root.to_external("Widget.cs"), "C:/repos/src/Widget.cs")

    def test_to_external_backslashes_normalised(self):
        root = self._root("C:/repos/src")
        self.assertEqual(root.to_external("sub\\Widget.cs"), "C:/repos/src/sub/Widget.cs")

    def test_to_local_uses_native_path(self):
        from indexserver import config as _cfg
        root = self._root("C:/repos/src")
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=True):
            mock_sys.platform = "linux"
            self.assertEqual(root.to_local("sub/Widget.cs"), "/mnt/c/repos/src/sub/Widget.cs")

    def test_to_local_docker_path_unchanged(self):
        root = self._root("/source/default")
        # On any platform, /source/default is already a native path
        self.assertEqual(root.to_local("sub/Widget.cs"), "/source/default/sub/Widget.cs")

    def test_to_local_strips_leading_slash(self):
        root = self._root("/source/default")
        self.assertEqual(root.to_local("/sub/Widget.cs"), "/source/default/sub/Widget.cs")

    def test_to_local_backslashes_normalised(self):
        root = self._root("/source/default")
        self.assertEqual(root.to_local("sub\\Widget.cs"), "/source/default/sub/Widget.cs")

    def test_native_path_property(self):
        from indexserver import config as _cfg
        root = self._root("C:/repos/src")
        with patch.object(_cfg, "_sys") as mock_sys, \
             patch.object(_cfg, "_is_wsl", return_value=True):
            mock_sys.platform = "linux"
            self.assertEqual(root.native_path, "/mnt/c/repos/src")


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

    def test_windows_path_unchanged(self):
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


if __name__ == "__main__":
    unittest.main()
