"""Server-side configuration: reads config.json from the parent codesearch directory."""

import json
import os
import re as _re
import sys as _sys

HOST = "localhost"

# config.json lives one level up (codesearch/config.json)
_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
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
API_PORT: int = PORT + 1   # management API (indexserver/api.py)
API_KEY: str = _CONFIG.get("api_key", "codesearch-local")

# ── Roots ─────────────────────────────────────────────────────────────────────
# Supports two formats for each root entry in config.json:
#   Old (string): "default": "/source/default"
#   New (object): "default": {"local_path": "/source/default", "windows_path": "C:/repos/src"}
#
# ROOTS      — path used to find files (local_path in container, string value in WSL)
# HOST_ROOTS — original Windows path stored as relative_path prefix in indexed docs

def _parse_roots(raw: dict) -> tuple[dict, dict]:
    """Parse roots config.  Each entry must be an object with at least one of:
      local_path   — path as seen by the current process (container or WSL)
      windows_path — original Windows path (used as the relative_path prefix in indexed docs)

    ROOTS uses local_path when present, otherwise windows_path.
    HOST_ROOTS is populated only when windows_path is set.
    """
    local_paths: dict[str, str] = {}
    windows_paths: dict[str, str] = {}
    for name, val in raw.items():
        lp = val.get("local_path", "").replace("\\", "/").rstrip("/")
        wp = val.get("windows_path", "").replace("\\", "/").rstrip("/")
        local_paths[name] = lp or wp
        if wp:
            windows_paths[name] = wp
    return local_paths, windows_paths

ROOTS, HOST_ROOTS = _parse_roots(_CONFIG.get("roots", {}))

SRC_ROOT: str = ROOTS.get("default") or next(iter(ROOTS.values()), "")


def _sanitize_root_name(name: str) -> str:
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())


def collection_for_root(name: str = "default") -> str:
    return f"codesearch_{_sanitize_root_name(name)}"


def get_root(name: str = "") -> tuple[str, str]:
    """Resolve root name → (collection_name, src_path). Empty = first root."""
    if not name:
        name = "default" if "default" in ROOTS else next(iter(ROOTS))
    if name not in ROOTS:
        raise ValueError(f"Unknown root {name!r}. Available: {sorted(ROOTS)}")
    return collection_for_root(name), ROOTS[name]


_default_root_name = "default" if "default" in ROOTS else next(iter(ROOTS), "default")
COLLECTION: str = collection_for_root(_default_root_name)

TYPESENSE_VERSION = "27.1"

INCLUDE_EXTENSIONS = {
    ".cs",
    ".cpp", ".c", ".h", ".hpp", ".idl",
    ".dsc", ".inc", ".props", ".targets", ".csproj",
    ".py", ".sh", ".cmd", ".bat", ".ps1",
    ".ts", ".js", ".json", ".xml", ".yaml", ".yml",
    ".md", ".txt",
    ".sql",
}

EXCLUDE_DIRS = {
    "Target", "Build", "Import", "nugetcache",
    ".git", "obj", "bin", "node_modules", ".venv",
    "target", "debug", "ship", "x64", "x86",
    "__pycache__", ".vs",
}

MAX_FILE_BYTES = 512 * 1024
MAX_CONTENT_CHARS = 30000

TYPESENSE_CLIENT_CONFIG = {
    "nodes": [{"host": HOST, "port": str(PORT), "protocol": "http"}],
    "api_key": API_KEY,
    "connection_timeout_seconds": 5,
}


def _is_wsl() -> bool:
    """Detect if running in WSL (vs native Linux like Docker)."""
    # WSL sets WSL_DISTRO_NAME environment variable
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    # Alternative check: WSL interop file exists
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
