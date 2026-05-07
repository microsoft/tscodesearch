"""Server-side configuration: reads config.json from the parent codesearch directory."""

import json
import os
import re as _re
from dataclasses import dataclass, field
from pathlib import Path


# ── Path normalization ────────────────────────────────────────────────────────

def normalize_path(path: str) -> str:
    """Canonical form for any filesystem path: backslashes → forward slashes."""
    return path.replace("\\", "/")


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


# ── Collection naming ─────────────────────────────────────────────────────────

def _sanitize_root_name(name: str) -> str:
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())


def collection_for_root(name: str = "default") -> str:
    return f"codesearch_{_sanitize_root_name(name)}"


# ── Repo location ─────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def index_root() -> Path:
    """Where Tantivy index directories live: <repo>/.tantivy/."""
    return _REPO_ROOT / ".tantivy"


# ── Root ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Root:
    """All configuration for one indexed source tree."""
    name: str             # logical root name (the key in config.json "roots")
    path: str             # forward-slash absolute path (e.g. C:/repos/src)
    collection: str       # collection name, e.g. "codesearch_default"
    extensions: frozenset # file extensions to index; defaults to INCLUDE_EXTENSIONS

    @property
    def index_dir(self) -> str:
        """Directory on disk holding this root's Tantivy index."""
        d = index_root() / self.collection
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def to_local(self, rel: str) -> str:
        """Return an absolute path for a repo-relative path."""
        r = normalize_path(rel).lstrip("/")
        return self.path.rstrip("/") + "/" + r

    # Back-compat alias used by older callers; identical to ``to_local`` now.
    def to_external(self, rel: str) -> str:
        return self.to_local(rel)


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    """All configuration for one codesearch instance.

    Construct via ``load_config()`` rather than directly.
    """
    port: int
    api_key: str
    roots: dict   # dict[str, Root]
    include_extensions: frozenset = field(default_factory=lambda: INCLUDE_EXTENSIONS)
    exclude_dirs: frozenset = field(default_factory=lambda: EXCLUDE_DIRS)
    max_file_bytes: int = 3 * 1024 * 1024
    max_content_chars: int = 30000

    @property
    def src_root(self) -> str:
        root = self.roots.get("default") or next(iter(self.roots.values()), None)
        return root.path if root else ""

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
    """
    result: dict[str, Root] = {}
    for name, val in raw.items():
        if isinstance(val, str):
            p = normalize_path(val).rstrip("/")
            exts_raw = None
        else:
            p = normalize_path(val.get("path", "")).rstrip("/")
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
    """Read config.json and return a Config instance."""
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
