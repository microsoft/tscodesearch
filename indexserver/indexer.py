"""
Index source files into Typesense.
Uses tree-sitter to extract class/interface/method/property symbols.

Usage:
    python indexer.py [--resethard]
    python indexer.py --src /path/to/src --collection my_collection --resethard
"""

import os
import re
import sys
import time
import hashlib
import argparse

# Allow running as a standalone script: add claudeskills/ to path
_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

import typesense

from indexserver.config import (
    TYPESENSE_CLIENT_CONFIG, COLLECTION, SRC_ROOT,
    INCLUDE_EXTENSIONS, EXCLUDE_DIRS, MAX_FILE_BYTES,
    collection_for_root,
)
from query.dispatch import describe_file

def _to_native_path(path: str) -> str:
    """Convert a Windows-style path (C:/foo or C:\\foo) to the platform-native form.

    On WSL (Linux), converts to /mnt/c/foo so that open() works correctly.
    On Windows, converts forward slashes to backslashes.
    """
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        if os.sep == "/":
            # WSL: C:/foo/bar → /mnt/c/foo/bar
            return "/mnt/" + p[0].lower() + p[2:]
        else:
            # Windows: C:/foo/bar → C:\foo\bar
            return p.replace("/", "\\")
    return p


_SRC_ROOT_NATIVE = _to_native_path(SRC_ROOT)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS = [
    {"name": "id",               "type": "string"},
    {"name": "relative_path",    "type": "string"},
    {"name": "filename",         "type": "string"},
    {"name": "extension",        "type": "string", "facet": True},
    {"name": "language",         "type": "string", "facet": True},
    {"name": "subsystem",        "type": "string", "facet": True},
    {"name": "namespace",        "type": "string", "optional": True, "facet": True},
    {"name": "class_names",      "type": "string[]", "optional": True},
    {"name": "method_names",     "type": "string[]", "optional": True},
    {"name": "tokens",           "type": "string"},
    {"name": "mtime",            "type": "int64"},
    # Declaration fields
    {"name": "member_sigs",      "type": "string[]", "optional": True},
    # Type reference fields (each serves a specific uses_kind)
    {"name": "base_types",       "type": "string[]", "optional": True},
    {"name": "field_types",      "type": "string[]", "optional": True},
    {"name": "local_types",      "type": "string[]", "optional": True},
    {"name": "param_types",      "type": "string[]", "optional": True},
    {"name": "return_types",     "type": "string[]", "optional": True},
    {"name": "cast_types",       "type": "string[]", "optional": True},
    {"name": "type_refs",        "type": "string[]", "optional": True},
    # Call and access site fields
    {"name": "call_sites",       "type": "string[]", "optional": True},
    {"name": "member_accesses",  "type": "string[]", "optional": True},
    # Other
    {"name": "attr_names",       "type": "string[]", "optional": True, "facet": True},
    {"name": "usings",           "type": "string[]", "optional": True},
]


def build_schema(collection_name: str) -> dict:
    return {
        "name": collection_name,
        "fields": _SCHEMA_FIELDS,
        # Split tokens on C# syntax characters so that parameter types and
        # generic type arguments are individually searchable.
        # e.g. "Task<Widget> GetAsync(int id)"  →  Task  Widget  GetAsync  int  id
        # Requires ts index --resethard to recreate the collection with the new schema.
        "token_separators": ["(", ")", "<", ">", "[", "]", ",", ".",",","+","-","/","*","?"],
    }


SCHEMA = build_schema(COLLECTION)


# ---------------------------------------------------------------------------
# Metadata extraction — derives all flat fields from FileDescription structure
# ---------------------------------------------------------------------------

def _expand_type(t: str) -> list:
    """Extract all identifier names from a type expression (works for <> and [] generics)."""
    base = t.rsplit(".", 1)[-1] if "." in t else t
    names = [base] if base else []
    for ident in re.findall(r'[A-Za-z_]\w*', base):
        if ident != base:
            names.append(ident)
    return names


def flat_from_fd(fd) -> dict:
    """Derive all indexer flat fields from a FileDescription's structured data."""
    from query._util import _dedupe

    class_names = [c.name for c in fd.classes]

    base_types = []
    for c in fd.classes:
        for bt in c.bases:
            unqual = bt.rsplit(".", 1)[-1] if "." in bt else bt
            idx = unqual.find("<")
            base_types.append(unqual[:idx].strip() if idx >= 0 else unqual)

    method_names = [m.name for m in fd.methods]
    _NO_SIG_KINDS = {"field", "property", "event"}
    member_sigs = (
        [m.sig for m in fd.methods if m.sig and m.kind not in _NO_SIG_KINDS]
        + [f.sig for f in fd.fields if f.sig]
    )

    return_types = []
    param_types  = []
    for m in fd.methods:
        if m.return_type:
            return_types.extend(_expand_type(m.return_type))
        for pt in m.param_types:
            param_types.extend(_expand_type(pt))

    field_types = []
    for f in fd.fields:
        if f.field_type:
            field_types.extend(_expand_type(f.field_type))
    for m in fd.methods:
        if m.kind == "event" and m.sig and m.name:
            suffix = f" {m.name}"
            if m.sig.endswith(suffix) and len(m.sig) > len(suffix):
                event_type = m.sig[:-len(suffix)].strip()
                if event_type:
                    field_types.extend(_expand_type(event_type))

    call_sites      = [cs.name for cs in fd.call_site_infos]
    cast_types      = [t for ci in fd.cast_infos      for t in _expand_type(ci.target_type)]
    local_types     = [t for lv in fd.local_var_infos  for t in _expand_type(lv.var_type)]
    member_accesses = [ma.member for ma in fd.member_access_infos]

    type_refs = list(field_types) + list(param_types) + list(return_types) + list(base_types) + list(local_types)
    for cs in fd.call_site_infos:
        if cs.receiver and cs.receiver[0].isupper():
            type_refs.extend(_expand_type(cs.receiver))
    type_refs.extend(base_types)

    usings     = [i.module for i in fd.imports if i.module]
    attr_names = [a.attr_name for a in fd.attrs if a.attr_name]

    return {
        "namespace":       fd.namespace,
        "class_names":     _dedupe(class_names),
        "method_names":    _dedupe(method_names),
        "base_types":      _dedupe(base_types),
        "call_sites":      _dedupe(call_sites),
        "cast_types":      _dedupe(cast_types),
        "member_sigs":     _dedupe(member_sigs),
        "type_refs":       _dedupe(type_refs),
        "attr_names":      _dedupe(attr_names),
        "usings":          _dedupe(usings),
        "return_types":    _dedupe(return_types),
        "param_types":     _dedupe(param_types),
        "field_types":     _dedupe(field_types),
        "local_types":     _dedupe(local_types),
        "member_accesses": _dedupe(member_accesses),
    }




def extract_metadata(src_bytes: bytes, ext: str) -> dict:
    """Extract semantic metadata from source bytes for the given file extension."""
    return flat_from_fd(describe_file(src_bytes, ext))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_id(relative_path: str) -> str:
    return hashlib.md5(relative_path.replace("\\", "/").encode()).hexdigest()


def subsystem_from_path(relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    return parts[0] if parts else ""


_LANGUAGE: dict[str, str] = {
    # C#
    ".cs":    "csharp",
    # Python
    ".py":    "python",
    # TypeScript
    ".ts":    "typescript",  ".tsx":   "typescript",
    # JavaScript
    ".js":    "javascript",  ".jsx":   "javascript",
    ".mjs":   "javascript",  ".cjs":   "javascript",
    # Java
    ".java":  "java",
    # C / C++
    ".c":     "cpp",  ".h":     "cpp",
    ".cpp":   "cpp",  ".cc":    "cpp",  ".cxx":   "cpp",
    ".hpp":   "cpp",  ".hxx":   "cpp",
    # Go
    ".go":    "go",
    # Rust
    ".rs":    "rust",
    # SQL
    ".sql":   "sql",
    # Kotlin
    ".kt":    "kotlin",  ".kts":   "kotlin",
    # Swift
    ".swift": "swift",
    # PHP
    ".php":   "php",
    # Ruby
    ".rb":    "ruby",
    # Scala
    ".scala": "scala",
    # R
    ".r":     "r",
    # Dart
    ".dart":  "dart",
    # Lua
    ".lua":   "lua",
    # Haskell
    ".hs":    "haskell",
    # F#
    ".fs":    "fsharp",  ".fsx":   "fsharp",  ".fsi":   "fsharp",
    # Visual Basic
    ".vb":    "vb",
    # Objective-C
    ".m":     "objc",  ".mm":    "objc",
    # Elixir
    ".ex":    "elixir",  ".exs":  "elixir",
    # Shell
    ".sh":    "shell",  ".bash":  "shell",
    # PowerShell
    ".ps1":   "powershell",  ".psm1":  "powershell",  ".psd1":  "powershell",
    # Batch
    ".cmd":   "batch",  ".bat":   "batch",
    # IDL
    ".idl":   "idl",
}


def _file_language(ext: str) -> str:
    return _LANGUAGE.get(ext, "")


def should_skip_dir(dirname: str) -> bool:
    return dirname in EXCLUDE_DIRS or dirname.startswith(".")



def build_document(full_path: str, relative_path: str, host_root: str = "") -> dict:
    try:
        stat = os.stat(full_path)
        with open(full_path, "rb") as _f:
            src_bytes = _f.read()
    except OSError:
        return None

    ext = os.path.splitext(full_path)[1].lower()
    meta = flat_from_fd(describe_file(src_bytes, ext))

    _raw = src_bytes.decode("utf-8", errors="replace")
    tokens = " ".join(dict.fromkeys(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', _raw)))
    relative_path_norm = relative_path.replace("\\", "/")
    stored_path = (host_root.rstrip("/") + "/" + relative_path_norm) if host_root else relative_path_norm

    return {
        "id":               file_id(stored_path),
        "relative_path":    stored_path,
        "filename":         os.path.basename(full_path),
        "extension":        ext.lstrip("."),
        "language":         _file_language(ext),
        "subsystem":        subsystem_from_path(relative_path_norm),
        "namespace":        meta["namespace"],
        "class_names":      meta["class_names"],
        "method_names":     meta["method_names"],
        "tokens":           tokens,
        "mtime":            int(stat.st_mtime),
        "member_sigs":      meta["member_sigs"],
        "base_types":       meta["base_types"],
        "field_types":      meta["field_types"],
        "local_types":      meta["local_types"],
        "param_types":      meta["param_types"],
        "return_types":     meta["return_types"],
        "cast_types":       meta["cast_types"],
        "type_refs":        meta["type_refs"],
        "call_sites":       meta["call_sites"],
        "member_accesses":  meta["member_accesses"],
        "attr_names":       meta["attr_names"],
        "usings":           meta["usings"],
    }


# ---------------------------------------------------------------------------
# Collection management
# ---------------------------------------------------------------------------

def get_client():
    return typesense.Client(TYPESENSE_CLIENT_CONFIG)


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------

_EXPECTED_TOKEN_SEPARATORS = set(build_schema("_")["token_separators"])

def verify_schema(client, collection: str) -> tuple[bool, list[str]]:
    """Check a Typesense collection against the expected schema.

    Returns (exists, warnings):
      exists=False — collection not found (not yet indexed); warnings is empty.
      exists=True  — collection found; warnings lists any field/type mismatches.
    Does not raise; callers should log the warnings.
    """
    try:
        info = client.collections[collection].retrieve()
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str:
            return False, []   # collection simply doesn't exist yet
        return False, [f"could not retrieve collection {collection!r}: {e}"]

    warnings = []

    # ── field checks ──────────────────────────────────────────────────────────
    # Typesense treats 'id' as a built-in field and never returns it in the
    # collection's fields list — skip it to avoid a spurious warning.
    actual_fields = {f["name"]: f for f in info.get("fields", [])}
    for expected in _SCHEMA_FIELDS:
        name = expected["name"]
        if name == "id":
            continue
        if name not in actual_fields:
            warnings.append(f"field {name!r} missing from collection")
            continue
        actual = actual_fields[name]
        if actual.get("type") != expected.get("type"):
            warnings.append(
                f"field {name!r} type: expected {expected['type']!r}, "
                f"got {actual.get('type')!r}"
            )
        if bool(expected.get("facet")) != bool(actual.get("facet")):
            warnings.append(
                f"field {name!r} facet: expected {expected.get('facet', False)}, "
                f"got {actual.get('facet', False)}"
            )

    # ── token_separators check ────────────────────────────────────────────────
    actual_seps = set(info.get("token_separators", []))
    missing_seps = _EXPECTED_TOKEN_SEPARATORS - actual_seps
    extra_seps   = actual_seps - _EXPECTED_TOKEN_SEPARATORS
    if missing_seps:
        warnings.append(f"token_separators missing: {sorted(missing_seps)}")
    if extra_seps:
        warnings.append(f"token_separators unexpected: {sorted(extra_seps)}")

    return True, warnings


def verify_all_schemas(client) -> dict:
    """Verify schema for every configured root; print results to stdout.

    Returns a dict keyed by root name:
        {"ok": bool, "warnings": [str, ...], "collection": str}
    """
    from indexserver.config import ALL_ROOTS
    results = {}
    for root in ALL_ROOTS.values():
        exists, warnings = verify_schema(client, root.collection)
        results[root.name] = {
            "ok":                 exists and not warnings,
            "collection_exists":  exists,
            "warnings":           warnings,
            "collection":         root.collection,
        }
        if not exists:
            print(f"[schema] MISSING {root.collection} (not yet indexed)", flush=True)
        elif warnings:
            for w in warnings:
                print(f"[schema] WARN  {root.collection}: {w}", flush=True)
        else:
            print(f"[schema] OK    {root.collection}", flush=True)
    return results


def ensure_collection(client, resethard=False, collection=None):
    coll_name = collection or COLLECTION
    schema = build_schema(coll_name)

    # Typesense can return 503 "Not Ready" briefly after startup even after
    # /health reports OK.  After a hard reset the server may also refuse
    # connections until fully initialized.  Retry on any transient error.
    exists = True
    for attempt in range(8):
        try:
            client.collections[coll_name].retrieve()
            break
        except Exception as e:
            err_str = str(e).lower()
            is_transient = (
                "503" in err_str
                or "connection" in err_str
                or "timeout" in err_str
                or "not ready" in err_str
            )
            if is_transient and attempt < 7:
                print(f"  Typesense not ready yet (attempt {attempt + 1}/8), retrying in 5s...")
                time.sleep(5)
            else:
                exists = False
                break

    if exists and resethard:
        print(f"Dropping existing collection '{coll_name}'...")
        client.collections[coll_name].delete()
        exists = False

    if not exists:
        print(f"Creating collection '{coll_name}'...")
        client.collections.create(schema)
        print("Collection created.")
    else:
        print(f"Collection '{coll_name}' already exists.")


# ---------------------------------------------------------------------------
# Full index walk
# ---------------------------------------------------------------------------

def walk_source_files(src_root: str, extensions=None):
    """Yield (full_path, relative_path) for all source files, respecting .gitignore.

    extensions: set of lowercase extensions to include (e.g. {".cs", ".py"}).
                When None, the global INCLUDE_EXTENSIONS set is used.
    """
    import pathspec

    exts = extensions if extensions is not None else INCLUDE_EXTENSIONS
    src_root = _to_native_path(src_root)

    # Cache: abs_dir -> PathSpec | None
    _spec_cache: dict = {}

    def _load_spec(dirpath: str):
        if dirpath in _spec_cache:
            return _spec_cache[dirpath]
        gi = os.path.join(dirpath, ".gitignore")
        spec = None
        if os.path.isfile(gi):
            try:
                with open(gi, "r", encoding="utf-8", errors="replace") as f:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
            except OSError:
                pass
        _spec_cache[dirpath] = spec
        return spec

    def _is_ignored(full_path: str) -> bool:
        """Check all ancestor .gitignore files from src_root down to the item's parent."""
        rel_parts = os.path.relpath(full_path, src_root).replace("\\", "/").split("/")
        check_dir = src_root
        for i, part in enumerate(rel_parts):
            spec = _load_spec(check_dir)
            if spec and spec.match_file("/".join(rel_parts[i:])):
                return True
            if i < len(rel_parts) - 1:
                check_dir = os.path.join(check_dir, part)
        return False

    for dirpath, dirs, files in os.walk(src_root, topdown=True):
        dirs[:] = [
            d for d in dirs
            if not should_skip_dir(d)
            and not _is_ignored(os.path.join(dirpath, d))
        ]
        for filename in files:
            full_path = os.path.join(dirpath, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in exts:
                continue
            try:
                if os.path.getsize(full_path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if _is_ignored(full_path):
                continue
            rel = os.path.relpath(full_path, src_root).replace("\\", "/")
            yield full_path, rel


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def _flush(client, docs, verbose, collection=None):
    coll_name = collection or COLLECTION
    try:
        results = client.collections[coll_name].documents.import_(
            docs, {"action": "upsert"}
        )
        if verbose:
            failed = [r for r in results if not r.get("success")]
            for f in failed:
                print(f"  WARN: {f}")
    except Exception as e:
        print(f"  ERROR during batch import: {e}")


def index_file_list(
    client,
    file_pairs,
    coll_name: str,
    batch_size: int = 50,
    verbose: bool = False,
    on_progress=None,
    stop_event=None,
    host_root: str = "",
) -> tuple[int, int]:
    """Shared batch-upsert pipeline used by both the full indexer and the verifier.

    Args:
        client:      Typesense client.
        file_pairs:  Iterable of (full_path, relative_path) tuples.
        coll_name:   Typesense collection name.
        batch_size:  Documents per import batch.
        verbose:     Print per-document warnings on import failure.
        on_progress: Optional callable(n_indexed: int, n_errors: int) invoked
                     after every flushed batch.
        stop_event:  Optional threading.Event; when set the pipeline flushes
                     the current batch and returns early.

    Returns:
        (total_indexed, total_errors)
    """
    docs_batch: list[dict] = []
    total = 0
    errors = 0

    for full_path, rel in file_pairs:
        if stop_event and stop_event.is_set():
            break

        doc = build_document(full_path, rel, host_root=host_root)
        if doc is None:
            errors += 1
            continue

        docs_batch.append(doc)

        if len(docs_batch) >= batch_size:
            _flush(client, docs_batch, verbose, coll_name)
            total += len(docs_batch)
            docs_batch = []
            if on_progress:
                on_progress(total, errors)

    if docs_batch:
        _flush(client, docs_batch, verbose, coll_name)
        total += len(docs_batch)
        if on_progress:
            on_progress(total, errors)

    return total, errors


def walk_and_enqueue(
    src_root: str,
    collection: str,
    queue,
    resethard: bool = False,
    stop_event=None,
    extensions=None,
) -> tuple[int, int]:
    """Walk *src_root* and feed every source file into *queue*.

    Calls ensure_collection() first (dropping the collection when resethard=True).
    Returns (new_entries, deduped_entries).
    """
    src_root = _to_native_path(src_root)
    client = get_client()
    ensure_collection(client, resethard=resethard, collection=collection)
    return queue.enqueue_bulk(
        walk_source_files(src_root, extensions=extensions),
        collection=collection,
        stop_event=stop_event,
    )


def run_index(src_root=None, resethard=False, batch_size=50, verbose=False, collection=None, host_root=""):
    coll_name = collection or COLLECTION
    root_exts = None
    if src_root is None:
        # Derive src_root (and host_root if not supplied) from config using collection name.
        from indexserver.config import ALL_ROOTS
        for root in ALL_ROOTS.values():
            if root.collection == coll_name:
                src_root = root.local_path
                if not host_root:
                    host_root = root.external_path
                root_exts = root.extensions
                break
        if src_root is None:
            src_root = _SRC_ROOT_NATIVE
    src_root = _to_native_path(src_root)
    exts = root_exts if root_exts is not None else INCLUDE_EXTENSIONS
    client = get_client()
    ensure_collection(client, resethard=resethard, collection=coll_name)

    t0 = time.time()
    last_report_t = t0
    last_report_n = 0
    current_sub = ""
    total_indexed = 0
    total_errors  = 0

    print(f"Indexing source files under: {src_root}")
    print(f"Extensions: {', '.join(sorted(exts))}")
    print()

    def _tracked_files():
        """Yield (full_path, rel) from walk_source_files with subsystem logging."""
        nonlocal current_sub
        for full_path, rel in walk_source_files(src_root, extensions=exts):
            sub = subsystem_from_path(rel)
            if sub != current_sub:
                current_sub = sub
                elapsed = time.time() - t0
                print(f"  [{_fmt_time(elapsed)}] subsystem: {sub}  "
                      f"(total so far: {total_indexed})")
            yield full_path, rel

    def _rate_report(n: int, errs: int) -> None:
        nonlocal last_report_t, last_report_n, total_indexed, total_errors
        total_indexed = n
        total_errors  = errs
        now = time.time()
        if now - last_report_t >= 30:
            elapsed  = now - t0
            delta_n  = n - last_report_n
            delta_t  = now - last_report_t
            rate     = delta_n / delta_t if delta_t > 0 else 0
            print(f"  [{_fmt_time(elapsed)}] {n:,} files indexed  "
                  f"({rate:.0f} files/s)  errors={errs}")
            last_report_t = now
            last_report_n = n

    total_indexed, total_errors = index_file_list(
        client, _tracked_files(), coll_name,
        batch_size=batch_size, verbose=verbose,
        on_progress=_rate_report,
        host_root=host_root,
    )

    elapsed = time.time() - t0
    rate = total_indexed / elapsed if elapsed > 0 else 0
    print()
    print(f"Done in {_fmt_time(elapsed)}. "
          f"Indexed {total_indexed:,} files  ({rate:.0f} files/s)  "
          f"errors={total_errors}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Index source files into Typesense")
    ap.add_argument("--resethard", action="store_true",
                    help="Drop and recreate the collection first")
    ap.add_argument("--src", default=None,
                    help="Root directory to index (default: derived from --collection via config)")
    ap.add_argument("--collection", default=None,
                    help="Collection name (default: from config)")
    ap.add_argument("--host-root", default="",
                    help="Windows-side path prefix stored in indexed filenames (overrides config lookup)")
    ap.add_argument("--status", action="store_true",
                    help="Show index stats and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    coll = args.collection or COLLECTION
    if args.status:
        client = get_client()
        try:
            info = client.collections[coll].retrieve()
            n = info.get("num_documents", "?")
            print(f"Collection '{coll}': {n:,} documents indexed")
        except Exception as e:
            print(f"Cannot retrieve index stats: {e}")
    else:
        run_index(src_root=args.src, resethard=args.resethard, verbose=args.verbose,
                  collection=coll, host_root=args.host_root)
