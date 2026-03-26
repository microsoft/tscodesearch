"""Server-side configuration: reads config.json from the parent codesearch directory."""

import json
import os
import re as _re
import sys as _sys

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
API_PORT: int = PORT + 1   # management API (indexserver/api.py)
API_KEY: str = _CONFIG.get("api_key", "codesearch-local")


# ── Platform helpers (defined before _parse_roots so it can call them) ────────

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


# ── Roots ─────────────────────────────────────────────────────────────────────
# Each root entry in config.json should have both:
#   local_path   — path as seen by this process (WSL: /mnt/c/…, Docker: /source/…)
#   external_path — Windows-side path (C:/…), stored as relative_path prefix in index

def _parse_roots(raw: dict) -> tuple[dict, dict, dict]:
    """Parse roots config.  Each entry should have both:
      local_path   — path as seen by the current process (WSL: /mnt/c/…, Docker: /source/…)
      external_path — original Windows path (C:/…), stored as relative_path prefix in indexed docs

    If only external_path is provided, local_path is auto-derived:
      - In WSL: C:/foo/bar  →  /mnt/c/foo/bar
      - In Docker/native Linux: cannot auto-derive; falls back to external_path (add local_path
        explicitly in config.json when running Docker with non-standard mount points)

    Optional per-root field:
      extensions — list of file extensions to index for this root (e.g. [".cs", ".py"]).
                   When absent or empty, the global INCLUDE_EXTENSIONS set is used.

    ROOTS uses local_path (the server-side filesystem path to use for file access).
    HOST_ROOTS stores external_path (the Windows-side path used as relative_path prefix).
    ROOT_EXTENSIONS stores per-root extension sets (None means use global INCLUDE_EXTENSIONS).
    """
    local_paths: dict[str, str] = {}
    external_paths: dict[str, str] = {}
    root_extensions: dict[str, frozenset | None] = {}
    for name, val in raw.items():
        lp = val.get("local_path", "").replace("\\", "/").rstrip("/")
        wp = val.get("external_path", "").replace("\\", "/").rstrip("/")
        if not lp and wp:
            # Auto-derive local_path from external_path when not explicitly set.
            # In WSL, convert the Windows drive path to /mnt/<drive>/... form.
            m = _re.match(r"^([a-zA-Z]):(.*)", wp)
            if m and _sys.platform == "linux" and _is_wsl():
                lp = f"/mnt/{m.group(1).lower()}{m.group(2)}"
            else:
                lp = wp  # Docker/native: no conversion; add local_path explicitly
        local_paths[name] = lp
        if wp:
            external_paths[name] = wp
        exts = val.get("extensions")
        if exts:
            root_extensions[name] = frozenset(
                e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts
            )
        else:
            root_extensions[name] = None  # use global INCLUDE_EXTENSIONS
    return local_paths, external_paths, root_extensions

ROOTS, HOST_ROOTS, ROOT_EXTENSIONS = _parse_roots(_CONFIG.get("roots", {}))

SRC_ROOT: str = ROOTS.get("default") or next(iter(ROOTS.values()), "")


def _sanitize_root_name(name: str) -> str:
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())


def collection_for_root(name: str = "default") -> str:
    return f"codesearch_{_sanitize_root_name(name)}"


def extensions_for_root(name: str) -> frozenset:
    """Return the extension set for a root, falling back to the global INCLUDE_EXTENSIONS."""
    exts = ROOT_EXTENSIONS.get(name)
    return exts if exts is not None else INCLUDE_EXTENSIONS


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
    ".cpp", ".c", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".idl",
    ".dsc", ".inc", ".props", ".targets", ".csproj",
    ".py", ".sh", ".cmd", ".bat", ".ps1",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".json", ".xml", ".yaml", ".yml",
    ".rs",
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


