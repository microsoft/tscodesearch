"""Server-side configuration: reads config.json from the parent codesearch directory."""

import json
import os
import re as _re
import sys as _sys
from dataclasses import dataclass, field


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


# ── Extension and exclusion defaults ─────────────────────────────────────────

INCLUDE_EXTENSIONS: frozenset = frozenset({
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

EXCLUDE_DIRS: frozenset = frozenset({
    "Target", "Build", "Import", "nugetcache",
    ".git", "obj", "bin", "node_modules", ".venv",
    "target", "debug", "ship", "x64", "x86",
    "__pycache__", ".vs",
})

TYPESENSE_VERSION = "27.1"


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


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    """All configuration for one codesearch instance.

    Construct via ``load_config()`` rather than directly.
    The ``roots`` dict is semantically immutable even though Python dicts are mutable.
    """
    port: int
    api_key: str
    roots: dict   # dict[str, Root]
    host: str = "localhost"
    include_extensions: frozenset = field(default_factory=lambda: INCLUDE_EXTENSIONS)
    exclude_dirs: frozenset = field(default_factory=lambda: EXCLUDE_DIRS)
    max_file_bytes: int = 3 * 1024 * 1024
    max_content_chars: int = 30000
    typesense_version: str = TYPESENSE_VERSION

    @property
    def api_port(self) -> int:
        return self.port + 1

    @property
    def typesense_client_config(self) -> dict:
        return {
            "nodes": [{"host": self.host, "port": str(self.port), "protocol": "http"}],
            "api_key": self.api_key,
            "connection_timeout_seconds": 5,
        }

    @property
    def src_root(self) -> str:
        root = self.roots.get("default") or next(iter(self.roots.values()), None)
        return root.native_path if root else ""

    @property
    def collection(self) -> str:
        name = "default" if "default" in self.roots else next(iter(self.roots), "default")
        return collection_for_root(name)

    def get_root(self, name: str = "") -> Root:
        """Resolve root name → Root. Empty name resolves to 'default' or the first root."""
        if not name:
            name = "default" if "default" in self.roots else next(iter(self.roots))
        if name not in self.roots:
            raise ValueError(f"Unknown root {name!r}. Available: {sorted(self.roots)}")
        return self.roots[name]


# ── Roots parsing ─────────────────────────────────────────────────────────────

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


# ── load_config ───────────────────────────────────────────────────────────────

def load_config(config_file: str | None = None) -> Config:
    """Read config.json and return a Config instance.

    config_file: explicit path; if None, uses CODESEARCH_CONFIG env var or
                 the default config.json next to the repo root.
    Raises RuntimeError if 'port' is missing from the config file.
    """
    path = config_file or (
        os.environ.get("CODESEARCH_CONFIG")
        or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
        )
    )
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = {}

    if "port" not in raw:
        raise RuntimeError(f"'port' is required in {path}")

    return Config(
        port=int(raw["port"]),
        api_key=raw.get("api_key", "codesearch-local"),
        roots=_parse_roots(raw.get("roots", {})),
    )
