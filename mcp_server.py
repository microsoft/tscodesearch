"""
MCP server for code search.

Exposes Typesense full-text search and tree-sitter C# structural queries
as native Claude tools — no copy-paste, results go straight into context.

Runs via WSL Python (mcp.sh → ~/.local/mcp-venv, requires Python 3.10+).
Registered with:  setup_mcp.cmd  (run once from repo root)

Restart / reload instructions:
  - To restart Typesense + file watcher (does NOT affect this MCP process):
        ts.cmd restart

  - To pick up changes to THIS file (mcp_server.py), you must reload the
    VS Code window so the Claude Code extension restarts the MCP subprocess:
        Ctrl+Shift+P  →  "Developer: Reload Window"

Tools:
    search_code      - Typesense full-text / semantic search across the index (file-level)
    query_codebase   - Typesense pre-filter + AST post-filter; exact line results, ≤250 files
    query_single_file- tree-sitter AST query on one file (no search)
    ready            - Quick synchronous check: is the index up to date with disk?
    verify_index     - Start/stop/monitor a background repair scan
    service_status   - Check if Typesense is running and how many docs are indexed
    manage_service   - Start, stop, or restart the Typesense + indexserver processes
"""

from __future__ import annotations


import io
import json
import os
import sys
import urllib.request
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
# Add the repo root to sys.path so all modules are importable.
# Uses __file__ so this works regardless of where the repo is cloned.
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)

from mcp.server.fastmcp import FastMCP



import re as _re_module


def _queue_warning() -> str:
    """Return a warning line if there are files pending in the index queue, else ''."""
    try:
        from config import API_PORT, HOST, API_KEY as _API_KEY
        req = urllib.request.Request(
            f"http://{HOST}:{API_PORT}/status",
            headers={"X-TYPESENSE-API-KEY": _API_KEY},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            st = json.loads(r.read())
        q_depth        = st.get("queue", {}).get("depth", 0)
        indexer_running = st.get("indexer", {}).get("running", False)
        parts = []
        if q_depth > 0:
            parts.append(f"{q_depth} files queued")
        if indexer_running:
            parts.append("indexer walk in progress")
        if parts:
            return f"[WARNING: index has outstanding work — {', '.join(parts)}. Results may be incomplete.]\n\n"
    except Exception:
        pass
    return ""

def _normalize_files_glob(path: str, src_root: str | None = None) -> str:
    """Normalize a files= glob to a path usable by the current process.

    Accepts any of:
      - Win fwd-slash: c:/myproject/src/**/*.cs
      - Win backslash: c:\\myproject\\src\\**\\*.cs
      - WSL paths:     /mnt/c/myproject/src/**/*.cs
      - $SRC_ROOT:     $SRC_ROOT/myapp/**/*.cs  (substituted with src_root or SRC_ROOT)

    Delegates platform conversion to config.to_native_path().
    """
    from config import SRC_ROOT as _DEFAULT_SRC_ROOT, to_native_path
    effective_root = src_root or _DEFAULT_SRC_ROOT
    # Substitute $SRC_ROOT / ${SRC_ROOT} before any path normalisation
    path = path.replace("${SRC_ROOT}", effective_root).replace("$SRC_ROOT", effective_root)
    return to_native_path(path)


def _glob_too_broad_hint(glob_pattern: str, src_root: str, ext: str = "cs") -> str:
    """Return a hint listing immediate subdirectories of the glob base directory.

    Called when a files= glob matches too many files. Helps the caller narrow
    to a specific subdirectory rather than re-trying the full tree.
    """
    # Find the fixed prefix before the first wildcard
    star_idx = glob_pattern.find('*')
    base = glob_pattern[:star_idx].rstrip('/\\') if star_idx >= 0 else glob_pattern
    if not os.path.isdir(base):
        base = os.path.dirname(base)
    if not os.path.isdir(base):
        return ""
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
        subdirs = [e for e in entries if e.is_dir()]
        if not subdirs:
            return ""
        rel_base = os.path.relpath(base, src_root).replace('\\', '/')
        lines = [
            "  " + os.path.relpath(e.path, src_root).replace('\\', '/') + f"/**/*.{ext}"
            for e in subdirs
        ]
        return f"\nSubdirectories of {rel_base}/ — narrow your glob to one of these:\n" + "\n".join(lines)
    except OSError:
        return ""


def _search_too_broad_hint(result: dict, total: int, limit: int,
                           sub: str, ext: str, src_root: str) -> str:
    """Return a breakdown of hits by subdirectory when search_code returns too many results.

    When no sub= filter is set, uses the Typesense subsystem facet (already in the result)
    to show per-subsystem counts.  When sub= is already set, groups the returned hits by
    their second path component to suggest finer-grained globs.

    Returns a multiline string the caller should return instead of the full result list.
    """
    ext_str = (ext or "cs").lstrip(".")
    lines = [
        f"Found {total} results but limit={limit} — too many to show reliably.",
        "Repeat with a narrower sub= or files= glob instead of using grep.",
        "",
    ]

    if not sub:
        # First-level breakdown: use subsystem facet (already computed by Typesense)
        subsystem_counts: list[tuple[str, int]] = []
        for fc in result.get("facet_counts", []):
            if fc.get("field_name") == "subsystem":
                for c in fc.get("counts", []):
                    subsystem_counts.append((c["value"], int(c["count"])))

        if subsystem_counts:
            subsystem_counts.sort(key=lambda x: -x[1])
            lines.append("Hits by subsystem — call search_code again with sub=<name>:")
            for name, count in subsystem_counts[:25]:
                lines.append(f"  sub={name!r:<20}  {count:>5} hits")
        else:
            lines.append("(No subsystem facet data available — try adding sub= or ext= to narrow.)")
    else:
        # Second-level breakdown: group returned hits by second path component
        subdir_counts: dict[str, int] = {}
        for hit in result.get("hits", []):
            rel = hit["document"].get("relative_path", "").replace("\\", "/")
            parts = rel.split("/")
            # parts[0] = subsystem (already filtered), parts[1] = next level
            key = "/".join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else rel
            subdir_counts[key] = subdir_counts.get(key, 0) + 1

        shown = len(result.get("hits", []))
        if subdir_counts:
            lines.append(
                f"Hits by subdirectory (sample of {shown}/{total} results shown) "
                f"— call query_ast with files= glob:"
            )
            for path, count in sorted(subdir_counts.items(), key=lambda x: -x[1])[:25]:
                glob_path = src_root.replace("\\", "/").rstrip("/") + "/" + path + f"/**/*.{ext_str}"
                lines.append(f"  files={glob_path!r:<60}  {count}+ hits")
        else:
            lines.append("(Could not group hits — narrow your query or add ext= filter.)")

    lines.append("")
    lines.append("Do NOT fall back to grep. Repeat search_code with sub= or use query_ast with files= glob.")
    return "\n".join(lines)


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("tscodesearch")


@mcp.tool()
def search_code(
    query: str,
    sub:   str = "",
    ext:   str = "",
    limit: int = 20,
    mode:  str = "text",
    root:  str = "",
    debug: bool = False,
) -> str:
    """
    Search the code index (C#, C++, Python, etc.)

    PREFER THIS TOOL OVER GREP for all code searches. Use grep only when the
    index is unavailable or you need regex patterns not supported here.

    Args:
        query: Text or symbol to search for.
        sub:   Filter by subsystem — "myapp", "services", "core", etc.
               Leave empty to search all.
        ext:   Filter by extension — "cs", "h", "py". Default: all (.cs ranked first).
        limit: Maximum results to return. Default 20.
        mode:  Search strategy:
               "text"       — filename + class/method names + full content (default)
               "declarations" — declaration names only (class/interface/method names)
               "implements" — files where query type appears in base_types (T1 field)
               "calls"      — files where query method appears in call_sites (T1 field)
               "uses"       — files where query type appears in type annotation positions (T1/T2)
               "attrs"      — files decorated with query attribute name (T2)
               "casts"      — files with explicit (TYPE)expr casts to query type (T1)
        root:  Named source root to search (empty = default root).
               Configure roots in config.json under the "roots" key.
        debug: Show matched fields, full signature list, and raw match details per
               result. Use when a file appears in results but the matching reason
               is unclear (e.g. sig search shows unrelated signatures).
    """
    from search import search, format_results
    from config import get_root, ROOTS

    try:
        collection, _src_root = get_root(root)
    except ValueError as e:
        return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

    try:
        result, query_by = search(
            query        = query,
            ext          = ext   or None,
            sub          = sub   or None,
            limit        = limit,
            symbols_only = (mode == "declarations"),
            implements   = (mode == "implements"),
            calls        = (mode == "calls"),
            sig          = (mode == "sig"),
            uses         = (mode == "uses"),
            attrs        = (mode == "attrs"),
            casts        = (mode == "casts"),
            collection   = collection,
        )
    except SystemExit:
        return ("Typesense search failed. Is the server running?\n"
                "Start it with: ts start\n"
                "Check status with: ts status")

    total = result.get("found", 0)
    if total > limit:
        return _queue_warning() + _search_too_broad_hint(
            result, total, limit, sub, ext, _src_root
        )

    buf = io.StringIO()
    sys.stdout, old = buf, sys.stdout
    try:
        format_results(result, query, query_by, show_facets=True, debug=debug)
    finally:
        sys.stdout = old

    return _queue_warning() + (buf.getvalue().strip() or "No results found.")


_QUERY_CODEBASE_LIMIT = 250
_MAX_OUTPUT_CHARS = 40_000



@mcp.tool()
def query_codebase(
    mode:          str,
    pattern:       str,
    sub:           str = "",
    ext:           str = "",
    context_lines: int = 0,
    root:          str = "",
    include_body:  bool = False,
    symbol_kind:   str = "",
    uses_kind:     str = "",
) -> str:
    """
    Typesense pre-filter + tree-sitter AST in one call.
    Returns exact line-level results. NEVER returns partial results.

    If the search matches more than 250 files, returns an error with a
    per-subsystem breakdown — repeat with sub= to narrow, or use
    query_single_file for a specific file.

    For listing modes that enumerate file contents without filtering
    (methods, fields, classes, usings, imports) use query_single_file.

    Args:
        mode:    Search + AST mode. All modes run Typesense pre-filter then
                 tree-sitter AST to return exact line numbers.
                 C#:     text, declarations, calls, implements, uses, casts,
                         attrs, accesses_of, accesses_on, all_refs
                 Python: calls, implements, ident, declarations, params, decorators
        pattern: Type/method/name to search for. Used for both the
                 Typesense pre-filter and the AST query.
        sub:     Narrow to a subsystem — the FIRST path component only (the top-level
                 directory name). Sub-directories are NOT valid sub= values; always
                 use the immediate child of the source root.
        ext:     File extension ("cs" or "py"). Default: cs.
        context_lines: Surrounding source lines per match (like grep -C N).
        root:    Named source root (empty = default).
        include_body: For declarations mode — include the full method/type body instead
                 of signature only. Default False.
        symbol_kind: For declarations mode — restrict results to a specific declaration
                 kind. Accepted values: method, constructor, property, field,
                 event, class, interface, struct, enum, record, delegate,
                 type (all types), member (all members).
                 Also narrows the Typesense pre-filter.
        uses_kind: For uses mode — which annotation positions to search.
                 Values: all (default), field, param, return, cast, base.
                 all:    fields + params + return types + casts + base types
                 field:  only field/property declarations typed as TYPE
                 param:  only method parameters typed as TYPE
                 return: only methods returning TYPE
                 cast:   only explicit (TYPE)expr casts
                 base:   only types inheriting/implementing TYPE

    Examples:
        query_codebase("casts", "Widget")
        query_codebase("uses", "IDataStore", sub="core")
        query_codebase("uses", "BlobStore", uses_kind="param", sub="sts")
        query_codebase("uses", "BlobStore", uses_kind="field", sub="sts")
        query_codebase("calls", "SaveChanges", sub="services")
        query_codebase("implements", "IRepository")
        query_codebase("attrs", "Obsolete", sub="api")
        query_codebase("declarations", "SaveChanges", sub="core")
        query_codebase("accesses_of", "ConnectionString")
    """
    import json as _json
    import urllib.request as _urlreq
    import urllib.error as _urlerr
    from config import API_PORT, HOST, API_KEY as _API_KEY, ROOTS

    # Listing modes enumerate file contents without a meaningful pattern —
    # they belong in query_single_file, not in a codebase-wide search.
    _LISTING_MODES = frozenset(("methods", "fields", "classes", "usings", "imports"))
    m = mode.lower().strip().replace("-", "_")
    if m in _LISTING_MODES:
        return (f"Mode '{m}' lists file contents without filtering — "
                f"use query_single_file instead:\n"
                f"  query_single_file({m!r}, file='$SRC_ROOT/path/to/File.cs')")

    # Call the indexserver /query-codebase endpoint
    payload = _json.dumps({
        "mode":         m,
        "pattern":      pattern,
        "sub":          sub or "",
        "ext":          (ext or "cs").lstrip("."),
        "root":         root or "",
        "limit":        _QUERY_CODEBASE_LIMIT,
        "include_body": include_body,
        "symbol_kind":  symbol_kind or "",
        "uses_kind":    uses_kind or "",
    }).encode()

    req = _urlreq.Request(
        f"http://{HOST}:{API_PORT}/query-codebase",
        data=payload,
        headers={
            "X-TYPESENSE-API-KEY": _API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read())
    except _urlerr.HTTPError as e:
        try:
            body = _json.loads(e.read())
            err    = body.get("error", str(e))
            detail = body.get("detail", "")
        except Exception:
            err, detail = str(e), ""
        msg = f"TSCODESEARCH ERROR — do not fall back to Grep/Glob; investigate and fix.\nError from indexserver: {err}"
        if detail:
            msg += f"\nDetail: {detail}"
        return _queue_warning() + msg
    except Exception as e:
        return (_queue_warning() +
                f"TSCODESEARCH ERROR — do not fall back to Grep/Glob; investigate and fix.\n"
                f"Could not reach indexserver (port {API_PORT}): {e}\n"
                f"Is the server running? Start it with: ts start")

    found   = data.get("found", 0)
    hits    = data.get("hits", [])
    facets  = data.get("facet_counts", [])

    if data.get("overflow"):
        # Build per-subsystem breakdown from Typesense facets
        lines = [
            f"Too many files ({found}) — narrowing required.",
            f"Repeat with sub= to scope to one subsystem, then re-run.",
            "",
        ]
        if not sub:
            subsystem_counts: list[tuple[str, int]] = []
            for fc in facets:
                if fc.get("field_name") == "subsystem":
                    for c in fc.get("counts", []):
                        subsystem_counts.append((c["value"], int(c["count"])))
            if subsystem_counts:
                subsystem_counts.sort(key=lambda x: -x[1])
                lines.append(f"Subsystems with '{pattern}' hits — re-run with sub=<name>:")
                for name, count in subsystem_counts[:25]:
                    lines.append(f"  query_codebase({m!r}, {pattern!r}, sub={name!r})  "
                                 f"# ~{count} files")
            else:
                lines.append("(No subsystem facet available — add sub= or ext= to narrow.)")
        else:
            lines.append("(Already scoped to a subsystem — narrow further with a more specific query.)")
        lines.append("")
        lines.append("Use query_single_file for a specific known file.")
        return _queue_warning() + "\n".join(lines)

    if not hits:
        header = (f"[Typesense: {found} files | AST scanned: {found} "
                  f"| files with matches: 0]\n")
        return _queue_warning() + header + "No AST matches found."

    # Format results as text
    output_lines: list[str] = []
    for hit in hits:
        rel_path = hit["document"].get("relative_path", "")
        for match in hit.get("matches", []):
            line_num = match.get("line", 0)
            text     = match.get("text", "").rstrip()
            output_lines.append(f"{rel_path}:{line_num}: {text}")

    n_files_matched = len(hits)
    header = (f"[Typesense: {found} files | AST scanned: {found} "
              f"| files with matches: {n_files_matched}]\n")

    output = "\n".join(output_lines)
    if not output:
        return _queue_warning() + header + "No AST matches found."

    # Guard against oversized results that would exceed MCP token limits.
    _MAX_CHARS = _MAX_OUTPUT_CHARS
    if len(output) > _MAX_CHARS:
        import datetime
        log_dir = Path.home() / ".local" / "tscodesearch" / "query_results"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"query_{ts}.txt"
        log_path.write_text(
            f"# query_codebase({mode!r}, {pattern!r}, sub={sub!r})\n\n{output}",
            encoding="utf-8",
        )
        truncated = output[:_MAX_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        n_shown = truncated.count("\n") + 1
        n_total = len(output_lines)
        summary = (
            f"[Result truncated — {n_total} matches across {n_files_matched} files "
            f"({len(output):,} chars). Full results saved to:\n"
            f"  {log_path}\n"
            f"Showing first {n_shown} lines below.]\n\n"
        )
        return _queue_warning() + header + summary + truncated

    return _queue_warning() + header + output


@mcp.tool()
def query_single_file(
    mode:          str,
    pattern:       str = "",
    file:          str = "",
    context_lines: int = 0,
    root:          str = "",
    include_body:  bool = False,
    symbol_kind:   str = "",
    uses_kind:     str = "",
) -> str:
    """
    Run a tree-sitter AST query on a single file. No Typesense search.

    Supports all modes including listing modes (methods, fields, classes,
    usings, imports) that enumerate file contents without a pattern.
    Works well on large source files — tree-sitter parses the whole file
    in memory and returns only the matching nodes.

    Args:
        mode:    AST query mode.
                 C# pattern-required: uses, calls, implements, casts, declarations,
                   attrs, accesses_of, accesses_on, all_refs, params
                 C# listing (no pattern): methods, fields, classes, usings
                 Python pattern-required: calls, implements, ident, declarations,
                   decorators, params
                 Python listing (no pattern): classes, methods, imports
        pattern: Type/method/name to search for. Required for pattern modes;
                 omit for listing modes.
        file:    Path to the file. Must be an absolute path. Accepts Windows
                 paths (e.g. q:/spocore/src/sts/foo.cs), WSL /mnt/ paths,
                 or $SRC_ROOT-prefixed paths (e.g. $SRC_ROOT/sts/foo.cs).
                 Relative paths (e.g. sts/foo.cs) are NOT supported and will
                 return "File not found".
        context_lines: Surrounding source lines per match (like grep -C N).
        root:    Named source root (empty = default).
        include_body: For declarations mode — include the full method/type body instead
                 of signature only. Default False.
        symbol_kind: For declarations mode — restrict to a specific declaration kind:
                 method, constructor, property, field, event, class,
                 interface, struct, enum, record, delegate, type, member.

    Examples:
        query_single_file("methods", file="$SRC_ROOT/services/OrderService.cs")
        query_single_file("classes", file="c:/myproject/src/services/OrderService.cs")
        query_single_file("casts", "Repository", file="$SRC_ROOT/services/OrderService.cs")
        query_single_file("declarations", "SaveChanges", file="$SRC_ROOT/data/Widget.cs")
        query_single_file("uses", "IRepository", file="$SRC_ROOT/services/OrderService.cs")
    """
    from query import process_file, process_py_file
    from config import get_root, ROOTS, to_native_path

    try:
        _collection, _src_root = get_root(root)
    except ValueError as e:
        return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

    _src_root = to_native_path(_src_root)

    if not file:
        return "file= is required."

    abs_path = _normalize_files_glob(file, src_root=_src_root)

    if not os.path.isfile(abs_path):
        return f"File not found: {abs_path}"

    _ext = os.path.splitext(abs_path)[1].lower()
    _lang = "py" if _ext == ".py" else "cs"

    m = mode.lower().strip().replace("-", "_")

    from query import process_file, process_py_file
    buf = io.StringIO()
    sys.stdout, _old = buf, sys.stdout
    try:
        fn = process_py_file if _lang == "py" else process_file
        fn(
            path         = abs_path,
            mode         = m,
            mode_arg     = pattern,
            show_path    = True,
            count_only   = False,
            context      = context_lines,
            src_root     = _src_root,
            include_body = include_body,
            symbol_kind  = symbol_kind,
            uses_kind    = uses_kind,
        )
    finally:
        sys.stdout = _old

    output = buf.getvalue().strip()
    rel = os.path.relpath(abs_path, _src_root).replace("\\", "/")
    header = f"[{rel}]\n"
    if not output:
        return header + "No matches found."

    _MAX_CHARS = _MAX_OUTPUT_CHARS
    if len(output) > _MAX_CHARS:
        import datetime
        log_dir = Path.home() / ".local" / "tscodesearch" / "query_results"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"query_{ts}.txt"
        log_path.write_text(
            f"# query_single_file({mode!r}, {pattern!r}, file={abs_path!r})\n\n{output}",
            encoding="utf-8",
        )
        truncated = output[:_MAX_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        n_shown = truncated.count("\n") + 1
        n_total = len(output.splitlines())
        summary = (
            f"[Result truncated — {n_total} lines ({len(output):,} chars). "
            f"Full results saved to:\n  {log_path}\n"
            f"Showing first {n_shown} lines below.]\n\n"
        )
        return header + summary + truncated

    return header + output


@mcp.tool()
def ready(root: str = "") -> str:
    """
    Check whether the code search index is fully up to date with the file system.

    Returns a quick status snapshot (no filesystem walk — returns immediately).
    Shows: Typesense health, document count, watcher state, queue depth, and the
    last verifier scan result (if any).

    To trigger a full repair scan use verify_index(action='start'), then poll
    ready() or verify_index(action='status') until complete.

    Args:
        root: Named source root to check (empty = default root).
    """
    import urllib.error
    from config import get_root, ROOTS, API_PORT, PORT, HOST, API_KEY as _API_KEY
    from config import collection_for_root

    try:
        _collection, _src_root = get_root(root)
    except ValueError as e:
        return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

    lines: list[str] = []

    # ── Typesense health + doc count ──────────────────────────────────────────
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{PORT}/health", timeout=10
        ) as r:
            health = json.loads(r.read())
        ts_ok = health.get("ok", False)
    except Exception as e:
        return f"Typesense is NOT running: {e}\nStart it with: ts start"

    lines.append(f"Typesense  : {'ok' if ts_ok else 'NOT OK'}")

    try:
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/collections/{_collection}",
            headers={"X-TYPESENSE-API-KEY": _API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            coll = json.loads(r.read())
        lines.append(f"Docs       : {coll.get('num_documents', '?'):,}  (collection: {_collection})")
    except Exception as e:
        lines.append(f"Collection : {_collection} — not found ({e})")

    # ── Indexserver status (watcher, queue, verifier) ─────────────────────────
    idx_req = urllib.request.Request(
        f"http://{HOST}:{API_PORT}/status",
        headers={"X-TYPESENSE-API-KEY": _API_KEY},
    )
    try:
        with urllib.request.urlopen(idx_req, timeout=10) as r:
            st = json.loads(r.read())

        watcher = st.get("watcher", {})
        queue   = st.get("queue", {})
        verifier = st.get("verifier", {})

        w_state = "running" if watcher.get("running") else ("paused" if watcher.get("paused") else "stopped")
        lines.append(f"Watcher    : {w_state}")

        q_depth = queue.get("depth", 0)
        q_total = queue.get("total_queued", 0)
        lines.append(f"Queue      : {q_depth} pending  ({q_total} total processed)")

        vp = verifier.get("progress", {})
        if vp:
            vstatus  = vp.get("status", "?")
            vphase   = vp.get("phase", "")
            missing  = vp.get("missing", 0)
            stale    = vp.get("stale", 0)
            orphaned = vp.get("orphaned", 0)
            total    = vp.get("total_to_update", 0)
            updated  = vp.get("updated", 0)
            remaining = max(0, total - updated) if total else (missing + stale)
            last     = vp.get("last_update", "?")
            lines.append(
                f"Verifier   : {vstatus}  phase={vphase}  "
                f"missing={missing}  stale={stale}  orphaned={orphaned}  "
                f"updated={updated}/{total}  (last: {last})"
            )
            left = remaining + q_depth
            if vstatus == "complete" and missing == 0 and stale == 0 and orphaned == 0 and q_depth == 0:
                lines.append("Left to index: 0  — index is up to date")
            elif vstatus == "running":
                lines.append(f"Left to index: ~{left}  ({remaining} verifier + {q_depth} queue) — poll again for updates")
            else:
                lines.append(f"Left to index: unknown — run verify_index(action='start') to check and repair")
        else:
            lines.append("Verifier   : no scan has been run yet")
            left = q_depth
            lines.append(f"Left to index: {left} queued — run verify_index(action='start') to check if index is complete")

    except Exception as e:
        lines.append(f"Indexserver: NOT running on port {API_PORT} — {e}\nStart it with: ts start")

    return "\n".join(lines)


@mcp.tool()
def verify_index(
    action:         str  = "status",
    root:           str  = "",
    delete_orphans: bool = True,
) -> str:
    """
    Verify that the code search index is up to date with the file system.

    Scans every source file on the server, compares modification times against
    the stored values, and re-indexes missing or stale files.  Orphaned index
    entries (files deleted from disk) are removed unless delete_orphans=False.

    Progress is visible via action='status' and also through the standard
    service_status tool / `ts status` CLI command.

    Args:
        action:         "start"  — launch a background verification scan.
                        "status" — show progress of the running or most recent scan.
                        "stop"   — cancel a running scan.
        root:           Named source root to verify (empty = default root).
        delete_orphans: When True (default), remove entries for deleted files.
    """
    import urllib.error
    from config import get_root, ROOTS, API_PORT, HOST, API_KEY as _API_KEY

    act = action.lower().strip()

    def _api(method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://{HOST}:{API_PORT}{path}",
            data=data,
            headers={"X-TYPESENSE-API-KEY": _API_KEY, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {"error": str(e)}
        except Exception as e:
            return 0, {"error": f"Could not reach indexserver on port {API_PORT}: {e}\nStart it with: ts start"}

    # ── stop ──────────────────────────────────────────────────────────────────
    if act == "stop":
        code, result = _api("POST", "/verify/stop")
        if code == 404:
            return "No verification scan is currently running."
        if code != 200:
            return f"Stop failed ({code}): {result.get('error', result)}"
        return "Verification scan stopped."

    # ── status ────────────────────────────────────────────────────────────────
    if act == "status":
        code, result = _api("GET", "/verify/status")
        if code == 404:
            return "No verification scan has been run. Use action='start' to begin."
        if code != 200:
            return f"Status failed ({code}): {result.get('error', result)}"

        def _fmt(v):
            return f"{v:,}" if isinstance(v, int) else str(v)

        running = result.get("running", False)
        lines = []
        if running:
            total = result.get("total_to_update", 0)
            done  = result.get("updated", 0)
            pct   = f"{done * 100 // total}%" if total else "—"
            lines.append(f"Running  : yes  ({pct} complete)")
        lines += [
            f"Status   : {result.get('status', '?')}",
            f"Phase    : {result.get('phase', '?')}",
            f"Started  : {result.get('started_at', '?')}",
            f"Updated  : {result.get('last_update', '?')}",
            f"FS files : {_fmt(result.get('fs_files', '?'))}",
            f"Index    : {_fmt(result.get('index_docs', '?'))} docs",
            f"Missing  : {result.get('missing', 0)}",
            f"Stale    : {result.get('stale', 0)}",
            f"Orphaned : {result.get('orphaned', 0)}",
            f"Re-indexed: {result.get('updated', 0)}",
            f"Deleted  : {result.get('deleted', 0)} orphans removed",
            f"Errors   : {result.get('errors', 0)}",
        ]
        return "\n".join(lines)

    # ── start ─────────────────────────────────────────────────────────────────
    if act == "start":
        try:
            collection, src_root = get_root(root)
        except ValueError as e:
            return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

        code, result = _api("POST", "/verify/start", {
            "root": root or "default",
            "delete_orphans": delete_orphans,
        })

        if code == 409:
            return (
                "A verification scan is already running.\n"
                "Use action='status' to monitor, or action='stop' to cancel."
            )
        if code != 200:
            return f"Start failed ({code}): {result.get('error', result)}"

        return (
            f"Verification scan started.\n"
            f"Root      : '{root or 'default'}' → {src_root}\n"
            f"Collection: {collection}\n"
            f"Use action='status' to monitor progress.\n"
            f"The standard service_status tool also shows the running job."
        )

    return f"Unknown action: {action!r}. Use 'start', 'status', or 'stop'."


@mcp.tool()
def manage_service(action: str = "status") -> str:
    """
    Start, stop, restart, check status, or rebuild the Typesense code search service.

    Manages the background processes: Typesense server (port 8108) and the
    indexserver/watcher (port 8109).  Both must be running for full functionality
    (search works with just Typesense; ready/verify_index also need the indexserver).

    Args:
        action: One of:
                "start"   — Start Typesense + indexserver (watcher + heartbeat).
                "stop"    — Stop both services.
                "restart" — Stop then start (use after config changes).
                "status"  — Show detailed service status (PID, docs, watcher state).
                "rebuild" — Wipe the index and re-index everything from scratch.
                            Use this after major source tree changes or index corruption.
                            Equivalent to: ts index --resethard
                            Runs in the background; monitor with action='status'.
    """
    import subprocess

    valid = ("start", "stop", "restart", "status", "rebuild")
    act = action.lower().strip()
    if act not in valid:
        return f"Unknown action: {action!r}. Valid actions: {', '.join(valid)}"

    ts_sh = os.path.join(_THIS_DIR, "ts.sh")
    if not os.path.isfile(ts_sh):
        return f"ts.sh not found at {ts_sh}"

    if act == "rebuild":
        cmd = ["bash", ts_sh, "index", "--resethard"]
        timeout = 120
    else:
        cmd = ["bash", ts_sh, act]
        timeout = 150 if act in ("start", "restart") else 30

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and not output:
            return f"Service '{act}' failed (exit {result.returncode})."
        if act == "rebuild" and result.returncode == 0:
            output += "\n\nRe-indexing is running in the background. Use action='status' to monitor progress."
        return output or f"Service '{act}' completed (no output)."
    except subprocess.TimeoutExpired:
        return f"Service '{act}' timed out after {timeout}s."
    except Exception as e:
        return f"Failed to run service command: {e}"


@mcp.tool()
def service_status(root: str = "") -> str:
    """
    Check whether the Typesense code search service is running.
    Returns server health, document count, and whether the index is up to date.
    If not running, returns instructions to start it.

    Args:
        root: Named root to inspect (empty = show all configured roots).
    """
    from config import API_KEY, PORT, HOST, ROOTS, get_root

    url = f"http://{HOST}:{PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            health = json.loads(r.read())
    except Exception as e:
        return (f"Typesense is NOT running on port {PORT}.\n"
                f"Start it with: ts start\n"
                f"Error: {e}")

    if not health.get("ok"):
        return "Typesense responded but health check returned not-ok."

    # Determine which roots to report
    if root:
        try:
            root_items = [(root, get_root(root)[0])]
        except ValueError as e:
            return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"
    else:
        root_items = [(name, f"codesearch_{name.lower().replace('-','_')}") for name in ROOTS]
        # Use collection_for_root for proper sanitization
        from config import collection_for_root
        root_items = [(name, collection_for_root(name)) for name in ROOTS]

    # Fetch per-root collection status from the indexserver API (schema validated server-side)
    from config import API_PORT
    api_status: dict = {}
    api_collections: dict = {}
    try:
        api_req = urllib.request.Request(
            f"http://{HOST}:{API_PORT}/status",
            headers={"X-TYPESENSE-API-KEY": API_KEY},
        )
        with urllib.request.urlopen(api_req, timeout=3) as r:
            api_status = json.loads(r.read())
            api_collections = api_status.get("collections", {})
    except Exception:
        pass  # indexserver may not be running; fall back to doc-count-only below

    indexer_running = api_status.get("indexer", {}).get("running", False)

    lines = [f"Typesense running on port {PORT}."]
    for root_name, coll_name in root_items:
        coll_info = api_collections.get(root_name)
        if coll_info:
            ndocs      = coll_info.get("num_documents")
            warnings   = coll_info.get("schema_warnings") or []
            col_exists = coll_info.get("collection_exists", ndocs is not None)
            if not col_exists:
                if indexer_running:
                    docs_str = f"{ndocs:,} docs so far" if ndocs else "collection being created"
                    lines.append(f"Root '{root_name}' ({coll_name}): indexing in progress ({docs_str})")
                else:
                    lines.append(f"Root '{root_name}' ({coll_name}): not yet indexed — run: ts index")
            elif warnings:
                warn_str = "; ".join(warnings)
                lines.append(
                    f"Root '{root_name}' ({coll_name}): {ndocs:,} docs  "
                    f"[SCHEMA OUTDATED — {warn_str}]  "
                    f"run: ts index --root {root_name} --resethard"
                )
            else:
                lines.append(f"Root '{root_name}' ({coll_name}): {ndocs:,} docs")
        else:
            # indexserver not running — query Typesense directly for doc count only
            req = urllib.request.Request(
                f"http://{HOST}:{PORT}/collections/{coll_name}",
                headers={"X-TYPESENSE-API-KEY": API_KEY},
            )
            try:
                with urllib.request.urlopen(req, timeout=3) as r:
                    stats = json.loads(r.read())
                ndocs = stats.get("num_documents", "?")
                lines.append(f"Root '{root_name}' ({coll_name}): {ndocs:,} docs  (schema unverified — indexserver not running)")
            except Exception:
                lines.append(f"Root '{root_name}' ({coll_name}): collection not found — run: ts index --root {root_name} --resethard")

    # Support Docker: TYPESENSE_DATA env var overrides default location
    _run_dir = Path(os.environ.get("TYPESENSE_DATA", Path.home() / ".local" / "typesense"))
    _watcher_stats_path = _run_dir / "watcher_stats.json"
    try:
        wstats  = json.loads(_watcher_stats_path.read_text(encoding="utf-8"))
        u       = wstats.get("files_upserted", 0)
        d       = wstats.get("files_deleted", 0)
        last    = wstats.get("last_flush") or "never"
        started = wstats.get("started_at") or "unknown"
        lines.append(f"Watcher: {u} upserted, {d} deleted since {started} (last: {last})")
    except Exception:
        pass

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Support both stdio (default) and SSE transport (for Docker)
    # Set MCP_TRANSPORT=sse and MCP_PORT=3000 for SSE mode
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "sse":
        mcp_port = int(os.environ.get("MCP_PORT", "3000"))
        print(f"[mcp] Starting MCP server on http://0.0.0.0:{mcp_port}/sse", flush=True)

        # FastMCP SSE requires uvicorn - run the ASGI app directly
        import uvicorn
        uvicorn.run(
            mcp.sse_app(),
            host="0.0.0.0",
            port=mcp_port,
            log_level="info",
        )
    else:
        mcp.run()
