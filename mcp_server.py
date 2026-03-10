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
    search_code    - Typesense full-text / semantic search across the index
    query_ast       - tree-sitter structural C# query (uses/calls/implements/...)
    query_py       - tree-sitter structural Python query (classes/methods/calls/...)
    ready          - Quick synchronous check: is the index up to date with disk?
    verify_index   - Start/stop/monitor a background repair scan
    service_status - Check if Typesense is running and how many docs are indexed
    manage_service - Start, stop, or restart the Typesense + indexserver processes
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

# ── File resolution ───────────────────────────────────────────────────────────
# Re-use the shared files_from_search from query.py.
# Returns Windows native paths (SRC_ROOT + relative_path) for file I/O.

from query import files_from_search as _files_from_search

def _do_files_from_search(query: str, sub: str | None = None,
                            ext: str = "cs", limit: int = 50,
                            collection: str | None = None,
                            src_root: str | None = None) -> list[str]:
    """Delegate to the shared files_from_search."""
    return _files_from_search(query=query, sub=sub, ext=ext, limit=limit,
                               collection=collection, src_root=src_root)


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


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """
    Convert a glob pattern (supporting ** for recursive matching) to a regex.

    *   matches any character except /
    **  (or **/) matches any sequence of characters including /
    ?   matches any single character except /
    """
    import re as _re
    pattern = pattern.replace("\\", "/")
    parts   = _re.split(r"(\*\*/?|\*|\?)", pattern)
    rx      = ""
    for part in parts:
        if part in ("**/", "**"):
            rx += ".*"
        elif part == "*":
            rx += "[^/]*"
        elif part == "?":
            rx += "[^/]"
        else:
            rx += _re.escape(part)
    return _re.compile("^" + rx + "$", _re.IGNORECASE)


def _ts_search_then_filter(glob_pattern: str, ts_query: str,
                            limit: int = 250) -> tuple[list[str], int]:
    """
    Search Typesense for ts_query, then filter results in-memory against
    glob_pattern — no filesystem glob expansion required.

    Returns (matched_file_list, total_ts_hits).
    """
    ts_files = _files_from_search(query=ts_query, limit=min(limit, 250))
    rx       = _glob_to_regex(glob_pattern)
    matched  = [f for f in ts_files if rx.match(f.replace("\\", "/"))]
    return matched, len(ts_files)


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
               "symbols"    — class/interface/method names only
               "implements" — files where query type appears in base_types (T1 field)
               "callers"    — files where query method appears in call_sites (T1 field)
               "uses"       — files where query type appears in type declarations (T2)
               "sig"        — files where query appears in method signatures (T1)
               "attr"       — files decorated with query attribute name (T2)
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
            symbols_only = (mode == "symbols"),
            implements   = (mode == "implements"),
            callers      = (mode == "callers"),
            sig          = (mode == "sig"),
            uses         = (mode == "uses"),
            attr         = (mode == "attr"),
            collection   = collection,
        )
    except SystemExit:
        return ("Typesense search failed. Is the server running?\n"
                "Start it with: ts start\n"
                "Check status with: ts status")

    buf = io.StringIO()
    sys.stdout, old = buf, sys.stdout
    try:
        format_results(result, query, query_by, show_facets=True, debug=debug)
    finally:
        sys.stdout = old

    return _queue_warning() + (buf.getvalue().strip() or "No results found.")


@mcp.tool()
def query_ast(
    mode:         str,
    pattern:      str = "",
    search_query: str = "",
    search_sub:   str = "",
    files:        str = "",
    context_lines: int = 0,
    count_only:   bool = False,
    root:         str = "",
) -> str:
    """
    Structural C# AST query using tree-sitter.
    Semantically precise: skips comments and string literals, understands syntax.
    PREFER THIS TOOL OVER GREP for C# structural queries. Use instead of text
    search when you need exact type references or call sites.

    Args:
        mode:          Query type — one of:
                       "uses"            every type reference to TYPE in declarations
                       "calls"           every call site of METHOD.
                                         Accepts bare name ("Create") or qualified
                                         name ("Factory.Create") to restrict to
                                         a specific class.
                       "implements"      types that inherit or implement TYPE
                       "field_type"      fields/properties declared with TYPE (migration analysis)
                       "param_type"      method/constructor parameters typed as TYPE
                       "casts"           every explicit cast expression (TYPE)expr
                       "ident"           every identifier occurrence (semantic grep — skips comments/strings)
                       "member_accesses" all .Member accesses on locals/params declared as TYPE.
                                         Use to discover which properties callers read from a value,
                                         e.g. what fields are used from a result object after a factory call.
                       "methods"         all method/field/property signatures
                       "fields"          all field/property declarations with types
                       "classes"         all type declarations with base types
                       "find"            full source body of method/type named NAME
                       "params"          parameter list of METHOD
                       "attrs"           all [Attribute] decorators
                       "usings"          all using directives
        pattern:       The TYPE, METHOD, or NAME to search for.
                       Required for: uses, calls, implements, find, params, member_accesses.
                       Optional for: attrs (filters by attribute name when provided).
        search_query:  Typesense query to pre-filter files (STRONGLY RECOMMENDED).
                       Finds ~50 most relevant files via the index before parsing.
                       Example: use "Blobber" to find files mentioning Blobber.
        search_sub:    Subsystem to scope the Typesense pre-filter search.
                       This is the FIRST directory component of the file path
                       relative to the source root — not a deeply nested folder.
                       Example: "src/myapp/submodule/foo.cs" → search_sub="myapp".
        files:         Glob pattern for direct file query. Accepts Windows
                       forward-slash, Windows backslash, or WSL /mnt/ paths —
                       all are normalised automatically. $SRC_ROOT is substituted.
                       Examples: "$SRC_ROOT/myapp/services/**/*.cs"
                                 "c:/myproject/src/mymodule/**/*.cs"
                                 "c:\\myproject\\src\\mymodule\\**\\*.cs"
                       Use this for comprehensive searches (scans every file).
        context_lines: Surrounding source lines to show per match (like grep -C N).
        count_only:    Return match counts per file instead of full match text.
        root:          Named source root to query (empty = default root).

    Examples:
        query_ast("uses", "StorageProvider", search_query="StorageProvider", search_sub="myapp")
        query_ast("calls", "DeleteItems", search_query="DeleteItems", search_sub="myapp")
        query_ast("calls", "Factory.Create", files="$SRC_ROOT/myapp/**/*.cs")
        query_ast("implements", "IStorageProvider", search_query="IStorageProvider")
        query_ast("field_type", "StorageProvider", search_query="StorageProvider")
        query_ast("field_type", "IStorageProvider", search_query="IStorageProvider")
        query_ast("param_type", "StorageProvider", search_query="StorageProvider", search_sub="myapp")
        query_ast("member_accesses", "ResultType", files="$SRC_ROOT/myapp/**/*.cs")
        query_ast("methods", files="$SRC_ROOT/myapp/services/ItemProcessor.cs")
        query_ast("find", "DeleteItems", files="$SRC_ROOT/myservice/StorageApi.cs")
        query_ast("uses", "StorageProvider", search_query="StorageProvider", search_sub="myapp", count_only=True)
    """
    import glob as _glob
    from query import process_file
    from config import get_root, ROOTS

    try:
        _collection, _src_root = get_root(root)
    except ValueError as e:
        return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

    VALID_MODES = ("uses", "calls", "implements", "methods", "fields",
                   "classes", "find", "params", "attrs", "usings",
                   "field_type", "param_type", "casts", "ident", "member_accesses")

    m = mode.lower().strip().replace("-", "_")
    if m not in VALID_MODES:
        return f"Unknown mode: {mode!r}. Valid modes: {', '.join(VALID_MODES)}"

    _PATTERN_REQUIRED = ("uses", "calls", "implements", "find", "params",
                         "field_type", "param_type", "casts", "ident", "member_accesses")
    if m in _PATTERN_REQUIRED and not pattern:
        return (f"Mode '{m}' requires a pattern argument. "
                f"Example: query_ast('{m}', 'TypeOrMethodName', search_query='...')")

    # ── Resolve file list ─────────────────────────────────────────────────────
    _prefilter_note = ""

    if search_query:
        file_list = _do_files_from_search(
            search_query, sub=search_sub or None, limit=50,
            collection=_collection, src_root=_src_root,
        )
    elif files:
        files = _normalize_files_glob(files, src_root=_src_root)
        _FILE_LIMIT = 250
        file_list = []
        for _f in _glob.iglob(files, recursive=True):
            if os.path.isfile(_f):
                file_list.append(_f)
                if len(file_list) > _FILE_LIMIT:
                    break
        if not file_list:
            return f"No files found matching glob: {files}"
        if len(file_list) > _FILE_LIMIT:
            sq = pattern or "your search term"
            return (
                f"Glob matched >{_FILE_LIMIT} files — too broad for tree-sitter scanning.\n"
                f"Use search_query to pre-filter via Typesense instead:\n"
                f"  query_ast('{m}', '{pattern}', search_query='{sq}', search_sub='mymodule')\n"
                f"Or use search_code('{sq}') to locate relevant files first."
            )
        file_list.sort()
        _prefilter_note = f"[glob: {len(file_list)} files]\n"
    else:
        return ("Provide either search_query (recommended for large subsystems) "
                "or a files glob pattern.")

    if not file_list:
        return "No matching files found in index or on disk."

    # ── Run tree-sitter query ─────────────────────────────────────────────────
    buf = io.StringIO()
    sys.stdout, old = buf, sys.stdout
    match_counts: dict[str, int] = {}
    try:
        for fpath in file_list:
            n = process_file(
                path       = fpath,
                mode       = m,
                mode_arg   = pattern,
                show_path  = True,
                count_only = False,
                context    = context_lines,
                src_root   = _src_root,
            )
            if n:
                match_counts[fpath] = n
    finally:
        sys.stdout = old

    if count_only:
        rows = sorted(match_counts.items(), key=lambda x: -x[1])
        lines = [f"  {n:4d}  {os.path.basename(p)}" for p, n in rows]
        total = sum(match_counts.values())
        lines.append(f"\nTotal: {total} matches in {len(match_counts)} files "
                     f"(searched {len(file_list)} files)")
        return _prefilter_note + "\n".join(lines)

    output = buf.getvalue().strip()
    if not output:
        return (_prefilter_note or "") + f"No matches found (searched {len(file_list)} files)."

    # Cap output to ~200 lines to avoid context overflow
    output_lines = output.splitlines()
    if len(output_lines) > 200:
        output = "\n".join(output_lines[:200])
        output += f"\n\n[truncated — {len(output_lines) - 200} more lines]"

    return _prefilter_note + output


@mcp.tool()
def query_py(
    mode:          str,
    pattern:       str = "",
    search_query:  str = "",
    search_sub:    str = "",
    files:         str = "",
    context_lines: int = 0,
    count_only:    bool = False,
    root:          str = "",
) -> str:
    """
    Structural Python AST query using tree-sitter.
    Semantically precise: skips comments and string literals, understands syntax.
    PREFER THIS TOOL OVER GREP for Python structural queries. Use instead of
    text search when you need exact call sites, class hierarchies, etc.

    Args:
        mode:          Query type — one of:
                       "classes"    all class definitions with base classes
                       "methods"    all function/method definitions with signatures
                       "calls"      every call site of a function/method name
                       "implements" classes that inherit from the given base class
                       "ident"      every identifier occurrence (semantic grep)
                       "find"       full source body of function/class named NAME
                       "decorators" all decorators, optionally filtered by name
                       "imports"    all import statements
                       "params"     parameter list of a function
        pattern:       The name to search for.
                       Required for: calls, implements, ident, find, params.
                       Optional for: decorators (filters by decorator name when provided).
        search_query:  Typesense query to pre-filter files (STRONGLY RECOMMENDED).
                       Finds ~50 most relevant Python files before tree-sitter parsing.
                       Example: use "MyBaseClass" to find files mentioning MyBaseClass.
        search_sub:    Subsystem to scope the Typesense pre-filter search.
                       This is the FIRST directory component of the file path
                       relative to the source root — not a deeply nested folder.
                       Example: "src/myapp/submodule/foo.py" → search_sub="myapp".
        files:         Glob pattern for direct file query. $SRC_ROOT is substituted.
                       Examples: "$SRC_ROOT/myapp/**/*.py"
                       Use this for comprehensive searches (scans every file).
        context_lines: Surrounding source lines to show per match (like grep -C N).
        count_only:    Return match counts per file instead of full match text.
        root:          Named source root to query (empty = default root).

    Examples:
        query_py("classes", search_query="MyBaseClass")
        query_py("calls", "fetch_data", search_query="fetch_data", search_sub="myapp")
        query_py("implements", "BaseHandler", search_query="BaseHandler")
        query_py("methods", files="$SRC_ROOT/myapp/services/processor.py")
        query_py("find", "process", files="$SRC_ROOT/myapp/processor.py")
        query_py("decorators", "route", search_query="route", search_sub="myapp")
        query_py("params", "fetch_data", search_query="fetch_data")
    """
    import glob as _glob
    from query import process_py_file
    from config import get_root, ROOTS

    try:
        _collection, _src_root = get_root(root)
    except ValueError as e:
        return f"Error: {e}\nConfigured roots: {', '.join(sorted(ROOTS))}"

    VALID_MODES = ("classes", "methods", "calls", "implements", "ident",
                   "find", "decorators", "imports", "params")

    m = mode.lower().strip()
    if m not in VALID_MODES:
        return f"Unknown mode: {mode!r}. Valid modes: {', '.join(VALID_MODES)}"

    _PATTERN_REQUIRED = ("calls", "implements", "ident", "find", "params")
    if m in _PATTERN_REQUIRED and not pattern:
        return (f"Mode '{m}' requires a pattern argument. "
                f"Example: query_py('{m}', 'FunctionOrClassName', search_query='...')")

    # ── Resolve file list ─────────────────────────────────────────────────────
    _prefilter_note = ""

    if search_query:
        file_list = _do_files_from_search(
            search_query, sub=search_sub or None, ext="py", limit=50,
            collection=_collection, src_root=_src_root,
        )
    elif files:
        files = _normalize_files_glob(files, src_root=_src_root)
        _FILE_LIMIT = 250
        file_list = []
        for _f in _glob.iglob(files, recursive=True):
            if os.path.isfile(_f):
                file_list.append(_f)
                if len(file_list) > _FILE_LIMIT:
                    break
        if not file_list:
            return f"No files found matching glob: {files}"
        if len(file_list) > _FILE_LIMIT:
            sq = pattern or "your search term"
            return (
                f"Glob matched >{_FILE_LIMIT} files — too broad for tree-sitter scanning.\n"
                f"Use search_query to pre-filter via Typesense instead:\n"
                f"  query_py('{m}', '{pattern}', search_query='{sq}', search_sub='mymodule')\n"
                f"Or use search_code('{sq}', ext='py') to locate relevant files first."
            )
        file_list.sort()
        _prefilter_note = f"[glob: {len(file_list)} files]\n"
    else:
        return ("Provide either search_query (recommended for large codebases) "
                "or a files glob pattern.")

    if not file_list:
        return "No matching Python files found in index or on disk."

    # ── Run tree-sitter query ─────────────────────────────────────────────────
    buf = io.StringIO()
    sys.stdout, old = buf, sys.stdout
    match_counts: dict[str, int] = {}
    try:
        for fpath in file_list:
            n = process_py_file(
                path       = fpath,
                mode       = m,
                mode_arg   = pattern,
                show_path  = True,
                count_only = False,
                context    = context_lines,
                src_root   = _src_root,
            )
            if n:
                match_counts[fpath] = n
    finally:
        sys.stdout = old

    if count_only:
        rows = sorted(match_counts.items(), key=lambda x: -x[1])
        lines_out = [f"  {n:4d}  {os.path.basename(p)}" for p, n in rows]
        total = sum(match_counts.values())
        lines_out.append(f"\nTotal: {total} matches in {len(match_counts)} files "
                         f"(searched {len(file_list)} files)")
        return _prefilter_note + "\n".join(lines_out)

    output = buf.getvalue().strip()
    if not output:
        return (_prefilter_note or "") + f"No matches found (searched {len(file_list)} files)."

    output_lines = output.splitlines()
    if len(output_lines) > 200:
        output = "\n".join(output_lines[:200])
        output += f"\n\n[truncated — {len(output_lines) - 200} more lines]"

    return _prefilter_note + output


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
                            Equivalent to: ts index --reset
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
        cmd = ["bash", ts_sh, "index", "--reset"]
        timeout = 120
    else:
        cmd = ["bash", ts_sh, act]
        timeout = 90 if act in ("start", "restart") else 30

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

    lines = [f"Typesense running on port {PORT}."]
    for root_name, coll_name in root_items:
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/collections/{coll_name}",
            headers={"X-TYPESENSE-API-KEY": API_KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                stats = json.loads(r.read())
            ndocs        = stats.get("num_documents", "?")
            has_priority = any(f["name"] == "priority" for f in stats.get("fields", []))
            lines.append(
                f"Root '{root_name}' ({coll_name}): {ndocs:,} docs"
                + ("" if has_priority else "  [NO priority field — run: ts index --resethard]")
            )
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
