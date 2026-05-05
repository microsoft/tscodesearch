"""Server-side configuration: reads config.json from the parent codesearch directory."""

import json
import os
import re as _re
import sys as _sys
from dataclasses import dataclass

HOST = "localhost"

# config.json lives one level up (codesearch/config.json)
_CONFIG_FILE = (
    os.environ.get("CODESEARCH_CONFIG")
    or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
    )
)


def _read_config() -> dict:
    try:
        with open(_CONFIG_FILE) as _f:
            return json.load(_f)
    except (OSError, json.JSONDecodeError):
        return {}


_CONFIG = _read_config()

if "port" not in _CONFIG:
    raise RuntimeError(f"'port' is required in {_CONFIG_FILE}")
PORT: int = int(_CONFIG["port"])
API_PORT: int = PORT + 1   # management API (tsquery_server.py)
API_KEY: str = _CONFIG.get("api_key", "codesearch-local")


# ── Platform helpers ──────────────────────────────────────────────────────────

def _is_wsl() -> bool:
    """Detect if running in WSL (vs native Linux like Docker)."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    return False


def to_native_path(path: str) -> str:
    """Convert a path (Windows or WSL) to the native format for the current process.

    On native Linux (Docker): paths are used as-is (no /mnt/ conversion needed).
    On WSL:    converts X:/... or X:\\... to /mnt/x/...
    On Windows: converts /mnt/x/... to X:/..., leaves X:/... unchanged.
    Uses forward slashes on both platforms (valid on Windows too).
    """
    path = path.replace("\\", "/")

    if _sys.platform == "linux":
        # Native Linux (Docker) - paths already correct, no conversion
        if not _is_wsl():
            return path

        # WSL - convert Windows paths to /mnt/x/... format
        m = _re.match(r"^([a-zA-Z]):(.*)", path)
        if m:
            path = f"/mnt/{m.group(1).lower()}{m.group(2)}"
    else:
        # Windows - convert /mnt/x/... to X:/...
        m = _re.match(r"^/mnt/([a-zA-Z])/(.*)", path)
        if m:
            path = f"{m.group(1).upper()}:/{m.group(2)}"
    return path


# ── Extension and exclusion sets ─────────────────────────────────────────────
# Defined before Root so the global set can be stored directly in Root.extensions.

INCLUDE_EXTENSIONS = frozenset({
    ".cs",
    ".cpp", ".c", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".idl",
    ".dsc", ".inc", ".props", ".targets", ".csproj",
    ".py", ".sh", ".cmd", ".bat", ".ps1",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".xml", ".yaml", ".yml",
    ".rs",
    ".md", ".txt",
    ".sql",
})

EXCLUDE_DIRS = {
    "Target", "Build", "Import", "nugetcache",
    ".git", "obj", "bin", "node_modules", ".venv",
    "target", "debug", "ship", "x64", "x86",
    "__pycache__", ".vs",
}


# ── Collection naming ─────────────────────────────────────────────────────────

def _sanitize_root_name(name: str) -> str:
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())


def collection_for_root(name: str = "default") -> str:
    return f"codesearch_{_sanitize_root_name(name)}"


# ── Root ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Root:
    """All configuration for one indexed source tree."""
    name: str             # logical root name (the key in config.json "roots")
    path: str             # canonical path as stored in config.json (e.g. C:/repos/src)
    collection: str       # Typesense collection name, e.g. "codesearch_default"
    extensions: frozenset # file extensions to index; defaults to INCLUDE_EXTENSIONS

    @property
    def native_path(self) -> str:
        """Platform-native path for file I/O: converts C:/... to /mnt/... in WSL."""
        return to_native_path(self.path)

    def to_local(self, rel: str) -> str:
        """Convert a relative path to a locally openable absolute path (uses native_path)."""
        r = rel.replace("\\", "/").lstrip("/")
        return self.native_path.rstrip("/") + "/" + r

    def to_external(self, rel: str) -> str:
        """Convert a relative path to the canonical absolute path for clients (uses path)."""
        r = rel.replace("\\", "/").lstrip("/")
        return self.path.rstrip("/") + "/" + r


# ── Roots ─────────────────────────────────────────────────────────────────────

def _parse_roots(raw: dict) -> "dict[str, Root]":
    """Parse the 'roots' section of config.json into Root objects.

    Each entry is either a bare string path or an object with a ``path`` key:
      ``"default": "C:/repos/src"``
      ``"default": {"path": "C:/repos/src"}``
      ``"default": {"path": "C:/repos/src", "extensions": [".cs", ".py"]}``

    Optional per-root field:
      extensions — list of file extensions (e.g. [".cs", ".py"]); defaults to INCLUDE_EXTENSIONS.
    """
    result: dict[str, Root] = {}
    for name, val in raw.items():
        if isinstance(val, str):
            p = val.replace("\\", "/").rstrip("/")
            exts_raw = None
        else:
            p = val.get("path", "").replace("\\", "/").rstrip("/")
            exts_raw = val.get("extensions")
        if exts_raw:
            exts: frozenset = frozenset(
                e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts_raw
            )
        else:
            exts = INCLUDE_EXTENSIONS
        result[name] = Root(
            name=name,
            path=p,
            collection=collection_for_root(name),
            extensions=exts,
        )
    return result


ALL_ROOTS: dict[str, Root] = _parse_roots(_CONFIG.get("roots", {}))

SRC_ROOT: str = (
    ALL_ROOTS["default"].native_path if "default" in ALL_ROOTS
    else next((r.native_path for r in ALL_ROOTS.values()), "")
)

_default_root_name = "default" if "default" in ALL_ROOTS else next(iter(ALL_ROOTS), "default")
COLLECTION: str = collection_for_root(_default_root_name)


def get_root(name: str = "") -> Root:
    """Resolve root name → Root.  Empty name resolves to 'default' or the first root."""
    if not name:
        name = "default" if "default" in ALL_ROOTS else next(iter(ALL_ROOTS))
    if name not in ALL_ROOTS:
        raise ValueError(f"Unknown root {name!r}. Available: {sorted(ALL_ROOTS)}")
    return ALL_ROOTS[name]


TYPESENSE_VERSION = "27.1"

MAX_FILE_BYTES = 3 * 1024 * 1024
MAX_CONTENT_CHARS = 30000

TYPESENSE_CLIENT_CONFIG = {
    "nodes": [{"host": HOST, "port": str(PORT), "protocol": "http"}],
    "api_key": API_KEY,
    "connection_timeout_seconds": 5,
}
