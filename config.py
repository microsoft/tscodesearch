"""Shared configuration for Typesense search tooling."""

HOST = "localhost"

import json
import os
import re as _re

# ── config.json ───────────────────────────────────────────────────────────────
# Stores roots (or legacy src_root) and api_key. Written by setup_mcp.cmd.
#
# New multi-root format:
#   {"api_key": "codesearch-local", "roots": {"default": "C:/myproject/src", "other": "C:/other/src"}}
#
# Legacy single-root format (auto-promoted to roots.default):
#   {"src_root": "C:/myproject/src", "api_key": "codesearch-local"}
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

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
# Each root maps a name → source directory (Windows forward-slash path).
# Old-format config has a single "src_root" key; promote it to roots.default.
_raw_roots: dict = _CONFIG.get("roots") or {"default": _CONFIG.get("src_root", "")}

ROOTS: dict[str, str] = {
    name: path.replace("\\", "/").rstrip("/")
    for name, path in _raw_roots.items()
}

# Backward-compat global: the "default" root's src path
SRC_ROOT: str = ROOTS.get("default") or next(iter(ROOTS.values()), "")


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
    import sys as _sys
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


def _sanitize_root_name(name: str) -> str:
    """Convert a root name to a valid Typesense collection name segment."""
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())


def collection_for_root(name: str = "default") -> str:
    """Return the Typesense collection name for a given root name."""
    return f"codesearch_{_sanitize_root_name(name)}"


def get_root(name: str = "") -> tuple[str, str]:
    """Resolve a root name to (collection_name, src_root).

    Empty string uses the first configured root (preferring "default" if present).
    Raises ValueError for unknown names.
    """
    if not name:
        name = "default" if "default" in ROOTS else next(iter(ROOTS))
    if name not in ROOTS:
        raise ValueError(f"Unknown root {name!r}. Available: {sorted(ROOTS)}")
    return collection_for_root(name), ROOTS[name]


# Backward-compat global: the default root's collection name
_default_root_name = "default" if "default" in ROOTS else next(iter(ROOTS), "default")
COLLECTION: str = collection_for_root(_default_root_name)

TYPESENSE_VERSION = "27.1"

INCLUDE_EXTENSIONS = {
    # C# (full symbol extraction via tree-sitter)
    ".cs",
    # Native C/C++
    ".cpp", ".c", ".h", ".hpp", ".idl",
    # Build system
    ".dsc", ".inc", ".props", ".targets", ".csproj",
    # Scripts
    ".py", ".sh", ".cmd", ".bat", ".ps1",
    # Web/config
    ".ts", ".js", ".json", ".xml", ".yaml", ".yml",
    # Docs
    ".md", ".txt",
    # SQL
    ".sql",
}

EXCLUDE_DIRS = {
    "Target", "Build", "Import", "nugetcache",
    ".git", "obj", "bin", "node_modules", ".venv",
    "target", "debug", "ship", "x64", "x86",
    "__pycache__", ".vs",
}

MAX_FILE_BYTES = 512 * 1024   # skip files larger than 512 KB
MAX_CONTENT_CHARS = 30000     # truncate content stored in Typesense

TYPESENSE_CLIENT_CONFIG = {
    "nodes": [{"host": HOST, "port": str(PORT), "protocol": "http"}],
    "api_key": API_KEY,
    "connection_timeout_seconds": 5,
}
