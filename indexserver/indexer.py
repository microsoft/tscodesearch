"""
Index source files into the Tantivy backend.
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
from dataclasses import dataclass

# Allow running as a standalone script.
_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.backend import Backend, drop as drop_index
from indexserver.config import normalize_path
from query.dispatch import describe_file


# ---------------------------------------------------------------------------
# Walk result
# ---------------------------------------------------------------------------

@dataclass
class SourceFile:
    """A single file yielded by walk_source_files."""
    full_path: str
    rel: str
    mtime: int


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


# Languages whose namespace strings are dot-separated (C#, Python, Java, JS,
# TypeScript, Kotlin, Scala). Other separators (Rust/C++ ``::``, JS module
# paths with ``/``) can be added per-language in the AST extractor by
# returning a pre-split list; the indexer accepts either form.
def _split_namespace(ns) -> list[str]:
    """Return the searchable components of a namespace value.

    Accepts a plain string (``"Acme.Billing.Service"``) — split on ``.`` —
    or a pre-split list/tuple from a language whose namespaces use a
    different separator.
    """
    if not ns:
        return []
    if isinstance(ns, (list, tuple)):
        return [str(p) for p in ns if p]
    return [p for p in str(ns).split(".") if p]


def _split_filename(basename: str) -> list[str]:
    """Return the searchable components of a filename.

    Always includes the full basename so a search for ``Foo.cs`` matches
    exactly; also includes the stem (``Foo``) and extension (``cs``) so
    searches for either still hit. For multi-dot names like
    ``Widget.Test.cs`` every dot-separated piece is added.
    """
    if not basename:
        return []
    out = [basename]
    for piece in basename.split("."):
        if piece and piece not in out:
            out.append(piece)
    return out


def path_tokens_from_path(relative_path: str) -> list[str]:
    """Per-directory + filename tokens that make subpath search work.

    ``services/billing/Foo.cs`` →
        ["services", "billing", "Foo.cs", "Foo", "cs"]

    Each directory name is its own raw token, so a query for ``billing``
    finds every file under any ``billing/`` directory at any depth. The
    filename is split via ``_split_filename`` so both ``Foo`` and
    ``Foo.cs`` match.
    """
    norm = normalize_path(relative_path)
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return []
    seen: set = set()
    out: list = []
    # Per-directory tokens (every ancestor folder name).
    for d in parts[:-1]:
        if d not in seen:
            seen.add(d)
            out.append(d)
    # Filename components.
    for piece in _split_filename(parts[-1]):
        if piece not in seen:
            seen.add(piece)
            out.append(piece)
    return out


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
    # ``member_sigs`` is no longer stored in the index — the per-identifier
    # fields plus ``member_sig_tokens`` cover the search story, and AST
    # post-processing provides line-level output. The aggregated list still
    # lives in this Python dict so test/diagnostic callers can inspect what
    # the extractors built; ``build_document`` simply ignores the key.
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

    imports    = [i.module for i in fd.imports if i.module]
    attr_names = [a.attr_name for a in fd.attrs if a.attr_name]

    # member_sig_tokens — every identifier inside any member's signature
    # (attribute names, parameter names, default-value identifiers, generic
    # args, etc.), deduped file-wide. Each language's extractor produces a
    # ``sig_tokens`` list per MethodInfo / FieldInfo by walking the member
    # AST and skipping the body. Languages that don't yet emit them simply
    # contribute nothing.
    sig_tokens: list = []
    for m in fd.methods:
        sig_tokens.extend(m.sig_tokens)
    for f in fd.fields:
        sig_tokens.extend(f.sig_tokens)

    return {
        # ``namespace`` is multi-value raw — store each dot-separated
        # component as its own searchable token.
        "namespace":         _dedupe(_split_namespace(fd.namespace)),
        "class_names":       _dedupe(class_names),
        "method_names":      _dedupe(method_names),
        "base_types":        _dedupe(base_types),
        "call_sites":        _dedupe(call_sites),
        "cast_types":        _dedupe(cast_types),
        "member_sigs":       _dedupe(member_sigs),   # diagnostic only — not indexed
        "member_sig_tokens": _dedupe(sig_tokens),
        "type_refs":         _dedupe(type_refs),
        "attr_names":        _dedupe(attr_names),
        "imports":           _dedupe(imports),
        "return_types":      _dedupe(return_types),
        "param_types":       _dedupe(param_types),
        "field_types":       _dedupe(field_types),
        "local_types":       _dedupe(local_types),
        "member_accesses":   _dedupe(member_accesses),
        "tokens":            _dedupe(fd.all_refs),
    }


def extract_metadata(src_bytes: bytes, ext: str) -> dict:
    """Extract semantic metadata from source bytes for the given file extension."""
    return flat_from_fd(describe_file(src_bytes, ext))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_id(relative_path: str) -> str:
    return hashlib.md5(normalize_path(relative_path).encode()).hexdigest()


def path_segments_from_path(relative_path: str) -> list[str]:
    """Cumulative ancestor folders for filter+facet narrowing.

    "a/b/c/Foo.cs" -> ["a", "a/b", "a/b/c"]
    "Foo.cs"       -> []
    """
    parts = [p for p in normalize_path(relative_path).split("/") if p]
    if len(parts) < 2:
        return []
    out, acc = [], []
    for p in parts[:-1]:
        acc.append(p)
        out.append("/".join(acc))
    return out


_LANGUAGE: dict[str, str] = {
    ".cs":    "csharp",
    ".py":    "python",
    ".ts":    "typescript",  ".tsx":   "typescript",
    ".js":    "javascript",  ".jsx":   "javascript",
    ".mjs":   "javascript",  ".cjs":   "javascript",
    ".java":  "java",
    ".c":     "cpp",  ".h":     "cpp",
    ".cpp":   "cpp",  ".cc":    "cpp",  ".cxx":   "cpp",
    ".hpp":   "cpp",  ".hxx":   "cpp",
    ".go":    "go",
    ".rs":    "rust",
    ".sql":   "sql",
    ".kt":    "kotlin",  ".kts":   "kotlin",
    ".swift": "swift",
    ".php":   "php",
    ".rb":    "ruby",
    ".scala": "scala",
    ".r":     "r",
    ".dart":  "dart",
    ".lua":   "lua",
    ".hs":    "haskell",
    ".fs":    "fsharp",  ".fsx":   "fsharp",  ".fsi":   "fsharp",
    ".vb":    "vb",
    ".m":     "objc",  ".mm":    "objc",
    ".ex":    "elixir",  ".exs":  "elixir",
    ".sh":    "shell",  ".bash":  "shell",
    ".ps1":   "powershell",  ".psm1":  "powershell",  ".psd1":  "powershell",
    ".cmd":   "batch",  ".bat":   "batch",
    ".idl":   "idl",
}


def _file_language(ext: str) -> str:
    return _LANGUAGE.get(ext, "")


def should_skip_dir(dirname: str, exclude_dirs) -> bool:
    return dirname in exclude_dirs or dirname.startswith(".")


def build_document(full_path: str, relative_path: str) -> dict | None:
    """Return the flat document dict for one source file, or ``None`` if the
    file can't be read (deleted between walk and read, permission denied, …).
    Callers must check for None."""
    try:
        stat = os.stat(full_path)
        with open(full_path, "rb") as _f:
            src_bytes = _f.read()
    except OSError:
        return None

    ext = os.path.splitext(full_path)[1].lower()
    meta = flat_from_fd(describe_file(src_bytes, ext))

    relative_path_norm = normalize_path(relative_path)

    return {
        # Stored fields (retrievable from the index for display/filter).
        "id":               file_id(relative_path_norm),
        "relative_path":    relative_path_norm,
        "filename":         os.path.basename(full_path),
        "extension":        ext.lstrip("."),
        "language":         _file_language(ext),
        "path_segments":    path_segments_from_path(relative_path_norm),
        "mtime":            int(stat.st_mtime),
        # Search-only fields (indexed but stored=False — not retrievable).
        # The AST stage provides the actual line-level matches; the index
        # only needs these to decide which files are candidates.
        "path_tokens":      path_tokens_from_path(relative_path_norm),
        "namespace":        meta["namespace"],
        "class_names":      meta["class_names"],
        "method_names":     meta["method_names"],
        "tokens":           meta["tokens"],
        "member_sig_tokens": meta["member_sig_tokens"],
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
        "imports":          meta["imports"],
    }


# ---------------------------------------------------------------------------
# Backend management
# ---------------------------------------------------------------------------

def ensure_backend(cfg, collection: str, resethard: bool = False, write: bool = True) -> Backend:
    """Open (or create) the Tantivy backend for `collection`.

    With resethard=True the on-disk directory is wiped before opening.
    """
    root = next((r for r in cfg.roots.values() if r.collection == collection), None)
    if root is None:
        # Test paths sometimes use a collection that isn't tied to a root.
        from indexserver.config import index_root
        index_dir = str(index_root() / collection)
    else:
        index_dir = root.index_dir

    if resethard:
        print(f"Wiping existing index '{collection}' at {index_dir}…", flush=True)
        drop_index(index_dir)

    print(f"Opening index '{collection}' at {index_dir}", flush=True)
    return Backend(index_dir, write=write)


# ---------------------------------------------------------------------------
# Full index walk
# ---------------------------------------------------------------------------

def walk_source_files(src_root: str, cfg, extensions=None):
    """Yield SourceFile for all source files under src_root, respecting .gitignore."""
    import pathspec

    exts = extensions if extensions is not None else cfg.include_extensions
    src_root = normalize_path(src_root)

    _dir_specs: dict = {}

    def _get_specs(dirpath: str, entries: list) -> list:
        parent = os.path.dirname(dirpath)
        inherited = _dir_specs.get(parent, [])
        for e in entries:
            if e.name == ".gitignore":
                try:
                    if e.is_file(follow_symlinks=False):
                        with open(e.path, "r", encoding="utf-8", errors="replace") as f:
                            spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
                        result = inherited + [(dirpath, spec)]
                        _dir_specs[dirpath] = result
                        return result
                except OSError:
                    pass
                break
        _dir_specs[dirpath] = inherited
        return inherited

    def _is_ignored(path: str, specs: list) -> bool:
        for base_dir, spec in specs:
            rel = normalize_path(os.path.relpath(path, base_dir))
            if spec.match_file(rel):
                return True
        return False

    stack = [src_root]
    while stack:
        dirpath = stack.pop()
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            continue

        specs = _get_specs(dirpath, entries)

        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if not should_skip_dir(entry.name, cfg.exclude_dirs) and not _is_ignored(entry.path, specs):
                    stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in exts:
                    continue
                if _is_ignored(entry.path, specs):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if st.st_size > cfg.max_file_bytes:
                        continue
                    mtime = int(st.st_mtime)
                except OSError:
                    continue
                rel = normalize_path(os.path.relpath(entry.path, src_root))
                yield SourceFile(entry.path, rel, mtime)


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def _flush(backend: Backend, docs: list, verbose: bool) -> tuple[int, int]:
    if not docs:
        return 0, 0
    n_ok, n_failed = backend.upsert_many(docs)
    if verbose and n_failed:
        print(f"  WARN: {n_failed} of {len(docs)} failed", flush=True)
    return n_ok, n_failed


def index_file_list(
    backend: Backend,
    file_pairs,
    batch_size: int = 50,
    verbose: bool = False,
    on_progress=None,
    stop_event=None,
) -> tuple[int, int]:
    """Shared batch-upsert pipeline used by the full indexer and the verifier."""
    docs_batch: list[dict] = []
    total = 0
    errors = 0

    for full_path, rel in file_pairs:
        if stop_event and stop_event.is_set():
            break

        doc = build_document(full_path, rel)
        if doc is None:
            errors += 1
            continue

        docs_batch.append(doc)

        if len(docs_batch) >= batch_size:
            n_ok, n_fail = _flush(backend, docs_batch, verbose)
            total  += n_ok
            errors += n_fail
            docs_batch = []
            if on_progress:
                on_progress(total, errors)

    if docs_batch:
        n_ok, n_fail = _flush(backend, docs_batch, verbose)
        total  += n_ok
        errors += n_fail
        if on_progress:
            on_progress(total, errors)

    return total, errors


def export_index_map(backend: Backend) -> dict[str, int]:
    """Return {doc_id: mtime} for every document in *backend*."""
    return backend.export_id_mtime()


def walk_and_enqueue(
    src_root: str,
    collection: str,
    queue,
    cfg,
    resethard: bool = False,
    stop_event=None,
    extensions=None,
) -> tuple[int, int, int]:
    """Walk *src_root*, enqueue changed files, delete orphans from the index.

    Returns (new_entries, deduped_entries, orphans_deleted).
    """
    src_root = normalize_path(src_root)
    with ensure_backend(cfg, collection, resethard=resethard) as backend:
        index_map: dict[str, int] = {} if resethard else export_index_map(backend)
        remaining = set(index_map)
        n_new = n_dedup = 0

        for sf in walk_source_files(src_root, cfg, extensions=extensions):
            if stop_event and stop_event.is_set():
                break
            doc_id = file_id(sf.rel)
            remaining.discard(doc_id)
            if index_map.get(doc_id) == sf.mtime:
                continue
            if queue.enqueue(sf.full_path, sf.rel, collection, mtime=sf.mtime):
                n_new += 1
            else:
                n_dedup += 1

        n_deleted = backend.delete_many(list(remaining)) if remaining else 0
        return n_new, n_dedup, n_deleted


def run_index(cfg, src_root=None, resethard=False, batch_size=50, verbose=False, collection=None):
    coll_name = collection or cfg.collection
    root_exts = None
    if src_root is None:
        for root in cfg.roots.values():
            if root.collection == coll_name:
                src_root = root.path
                root_exts = root.extensions
                break
        if src_root is None:
            src_root = cfg.src_root
    src_root = normalize_path(src_root)
    exts = root_exts if root_exts is not None else cfg.include_extensions

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
        nonlocal current_sub
        for sf in walk_source_files(src_root, cfg, extensions=exts):
            parts = normalize_path(sf.rel).split("/", 1)
            top = parts[0] if len(parts) > 1 else ""
            if top != current_sub:
                current_sub = top
                elapsed = time.time() - t0
                print(f"  [{_fmt_time(elapsed)}] folder: {top}  "
                      f"(total so far: {total_indexed})")
            yield sf.full_path, sf.rel

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

    with ensure_backend(cfg, coll_name, resethard=resethard) as backend:
        total_indexed, total_errors = index_file_list(
            backend, _tracked_files(),
            batch_size=batch_size, verbose=verbose,
            on_progress=_rate_report,
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
    from indexserver.config import load_config
    _cfg = load_config()
    ap = argparse.ArgumentParser(description="Index source files into the Tantivy backend")
    ap.add_argument("--resethard", action="store_true",
                    help="Drop and recreate the index first")
    ap.add_argument("--src", default=None,
                    help="Root directory to index (default: derived from --collection via config)")
    ap.add_argument("--collection", default=None,
                    help="Collection name (default: from config)")
    ap.add_argument("--status", action="store_true",
                    help="Show index stats and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    coll = args.collection or _cfg.collection
    if args.status:
        backend = ensure_backend(_cfg, coll, resethard=False, write=False)
        print(f"Collection '{coll}': {backend.num_documents():,} documents indexed")
    else:
        run_index(_cfg, src_root=args.src, resethard=args.resethard, verbose=args.verbose,
                  collection=coll)
