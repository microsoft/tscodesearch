"""
Python MCP server for tscodesearch.

Runs on Windows via .client-venv/Scripts/python.exe (stdio transport).

Tools:
  query_codebase     - Typesense pre-filter + tree-sitter AST (via indexserver /query-codebase)
  query_single_file  - Tree-sitter AST on one file (direct import — no indexserver required)
  ready              - Quick index health snapshot
  verify_index       - Start/stop/monitor index repair scan
  service_status     - Typesense + indexserver status
  manage_service     - Docker container lifecycle (start/stop/restart/rebuild)
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).parent
sys.path.insert(0, str(_REPO))

from mcp.server.fastmcp import FastMCP
from query.dispatch import query_file

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = _REPO / "config.json"
    try:
        raw = json.loads(cfg_path.read_text())
    except Exception as e:
        raise RuntimeError(f"Cannot read config.json at {cfg_path}: {e}")
    if "port" not in raw:
        raise RuntimeError(f"'port' is required in {cfg_path}")
    return {
        "api_key":          raw.get("api_key", "codesearch-local"),
        "port":             int(raw["port"]),
        "roots":            raw.get("roots", {}),
        "docker_container": raw.get("docker_container", "codesearch"),
    }

_cfg             = _load_config()
_API_PORT        = _cfg["port"] + 1
_API_KEY         = _cfg["api_key"]
_ROOTS           = _cfg["roots"]
_DOCKER          = _cfg["docker_container"]

_MAX_OUTPUT_CHARS     = 40_000
_QUERY_CODEBASE_LIMIT = 250

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http(method: str, path: str, body=None, timeout: int = 120):
    url     = f"http://localhost:{_API_PORT}{path}"
    headers = {"X-TYPESENSE-API-KEY": _API_KEY}
    data    = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}

def _get(path: str, timeout: int = 10):
    return _http("GET", path, timeout=timeout)

def _post(path: str, body: dict, timeout: int = 120):
    return _http("POST", path, body=body, timeout=timeout)

# ── Config helpers ────────────────────────────────────────────────────────────

def _collection_for_root(name: str) -> str:
    return "codesearch_" + re.sub(r"[^a-z0-9_]", "_", name.lower())

def _get_root(name: str) -> tuple[str, str]:
    effective = name or ("default" if "default" in _ROOTS else next(iter(_ROOTS), ""))
    if not effective or effective not in _ROOTS:
        available = ", ".join(sorted(_ROOTS))
        raise ValueError(f"Unknown root '{name}'. Available: {available}")
    entry = _ROOTS[effective]
    ext_path = entry.get("path", "") if isinstance(entry, dict) else entry
    return _collection_for_root(effective), ext_path

def _to_windows_path(file_path: str) -> str:
    default_entry  = _ROOTS.get("default") or (next(iter(_ROOTS.values()), None))
    default_root   = ""
    if default_entry:
        ep = default_entry.get("path", "") if isinstance(default_entry, dict) else default_entry
        default_root = ep.replace("\\", "/").rstrip("/")

    p = file_path.replace("\\", "/")
    p = p.replace("${SRC_ROOT}", default_root).replace("$SRC_ROOT", default_root)

    m = re.match(r"^/mnt/([a-zA-Z])/(.*)", p)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"

    if re.match(r"^[A-Za-z]:", p):
        return p

    if default_root:
        return f"{default_root}/{p}"

    return p

def _rel_path(file_path: str, src_root: str) -> str:
    norm = file_path.replace("\\", "/")
    root = src_root.replace("\\", "/").rstrip("/")
    if norm.lower().startswith(root.lower() + "/"):
        return norm[len(root) + 1:]
    return norm

def _queue_warning() -> str:
    try:
        status, data = _get("/status", timeout=2)
        if status != 200 or not isinstance(data, dict):
            return ""
        depth   = data.get("queue", {}).get("depth", 0)
        running = data.get("syncer", {}).get("running", False)
        parts   = []
        if depth > 0:
            parts.append(f"{depth} files queued")
        if running:
            parts.append("syncer walk in progress")
        if parts:
            return f"[WARNING: index has outstanding work — {', '.join(parts)}. Results may be incomplete.]\n\n"
    except Exception:
        pass  # server may not be running; warnings are best-effort
    return ""

def _truncate(output: str) -> tuple[str, bool]:
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output, False
    trunc = output[:_MAX_OUTPUT_CHARS]
    nl    = trunc.rfind("\n")
    return (trunc[:nl] if nl > 0 else trunc), True

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("tscodesearch")

# ── query_codebase ────────────────────────────────────────────────────────────

@mcp.tool()
def query_codebase(
    mode: str,
    pattern: str,
    sub: str = "",
    ext: str = "",
    root: str = "",
    include_body: bool = False,
    symbol_kind: str = "",
    uses_kind: str = "",
) -> str:
    """Typesense pre-filter + tree-sitter AST in one call. Returns exact line-level results.
NEVER returns partial results. If the search matches more than 250 files, returns a
per-subsystem breakdown — repeat with sub= to narrow.

For listing modes (methods, fields, classes, usings, imports) use query_single_file.

Args:
  mode:         text, declarations, calls, implements, uses, casts, attrs,
                accesses_of, accesses_on, all_refs (C#);
                calls, implements, ident, declarations, params, decorators (Python)
  pattern:      Type/method/name to search for.
  sub:          Narrow to a subsystem (first path component only).
  ext:          File extension filter. Common values: "cs", "py", "cpp".
                For C/C++, "cpp" automatically includes header files (.h, .hpp, .hxx).
                Omit to search all indexed languages. Default: cs.
  context_lines: Surrounding source lines per match.
  root:         Named source root (empty = default).
  include_body: For declarations — include full body. Default false.
  symbol_kind:  For declarations — restrict to: method, class, interface, etc.
  uses_kind:    For uses — all, field, param, return, cast, base, locals.

Examples:
  query_codebase("calls", "SaveChanges", sub="services")
  query_codebase("uses", "IDataStore", uses_kind="param", sub="services")
  query_codebase("implements", "IRepository")
  query_codebase("declarations", "SaveChanges", symbol_kind="method")"""
    _LISTING = {"methods", "fields", "classes", "usings", "imports"}
    m = mode.lower().strip().replace("-", "_")
    if m in _LISTING:
        return (f"Mode '{m}' lists file contents without filtering — use query_single_file instead:\n"
                f'  query_single_file("{m}", file="$SRC_ROOT/path/to/File.cs")')

    try:
        status, data = _post("/query-codebase", {
            "mode": m, "pattern": pattern, "sub": sub or "",
            "ext": (ext or "").lstrip("."),
            "root": root or "", "limit": _QUERY_CODEBASE_LIMIT,
            "include_body": include_body,
            "symbol_kind": symbol_kind or "", "uses_kind": uses_kind or "",
        })
    except Exception as e:
        return f"Could not reach indexserver: {e}\nStart it with: ts start"

    warn = _queue_warning()

    if status == 503 and isinstance(data, dict) and data.get("loading"):
        return "Typesense is still starting up — retry in a few seconds.\nUse service_status() to check when it is ready."
    if status >= 400:
        err    = data.get("error", json.dumps(data)) if isinstance(data, dict) else str(data)
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        msg    = f"TSCODESEARCH ERROR — do not fall back to Grep/Glob; investigate and fix.\nError from indexserver: {err}"
        if detail:
            msg += f"\nDetail: {detail}"
        return warn + msg

    found  = data.get("found", 0)
    hits   = data.get("hits", [])
    facets = data.get("facet_counts", [])

    if data.get("overflow"):
        lines = [f"Too many files ({found}) — narrowing required.",
                 "Repeat with sub= to scope to one subsystem, then re-run.", ""]
        if not sub:
            counts = []
            for fc in facets:
                if fc.get("field_name") == "subsystem":
                    for c in fc.get("counts", []):
                        counts.append((c["value"], int(c["count"])))
            if counts:
                counts.sort(key=lambda x: -x[1])
                lines.append(f"Subsystems with '{pattern}' hits — re-run with sub=<name>:")
                for name, count in counts[:25]:
                    lines.append(f'  query_codebase("{m}", "{pattern}", sub="{name}")  # ~{count} files')
        lines += ["", "Use query_single_file for a specific known file."]
        return warn + "\n".join(lines)

    header = f"[Typesense: {found} files | AST scanned: {found} | files with matches: {len(hits)}]\n"
    if not hits:
        return warn + header + "No AST matches found."

    out_lines = []
    for hit in hits:
        rel = (hit.get("document") or {}).get("relative_path", "")
        for match in hit.get("matches") or []:
            out_lines.append(f"{rel}:{match['line']}: {(match.get('text') or '').rstrip()}")
    output = "\n".join(out_lines)
    if not output:
        return warn + header + "No AST matches found."

    output, truncated = _truncate(output)
    if truncated:
        shown   = output.count("\n") + 1
        summary = f"[Result truncated — {len(out_lines)} matches. Showing first {shown} lines.]\n\n"
        return warn + header + summary + output
    return warn + header + output

# ── query_single_file ─────────────────────────────────────────────────────────

@mcp.tool()
def query_single_file(
    mode: str,
    pattern: str = "",
    file: str = "",
    root: str = "",
    include_body: bool = False,
    symbol_kind: str = "",
    uses_kind: str = "",
    head_limit: int = 250,
    offset: int = 0,
) -> str:
    """Run a tree-sitter AST query on a single file. No Typesense search.

Supports all modes including listing modes (methods, fields, classes, usings, imports).
Works well on large source files — tree-sitter parses the whole file and returns only matching nodes.

Args:
  mode:    AST query mode.
           C# pattern-required: uses, calls, implements, casts, declarations,
             attrs, accesses_of, accesses_on, all_refs, params
           C# listing (no pattern): methods, fields, classes, usings
           Python pattern-required: calls, implements, ident, declarations, decorators, params
           Python listing (no pattern): classes, methods, imports
  pattern: Type/method/name to search for. Omit for listing modes.
  file:    Absolute path to the file. Accepts Windows paths (C:/…), /mnt/c/… paths,
           or $SRC_ROOT-prefixed paths. Relative paths are NOT supported.
  context_lines: Surrounding source lines per match.
  root:    Named source root (empty = default).
  include_body: For declarations — include full body. Default false.
  symbol_kind:  For declarations — restrict to a specific kind.
  uses_kind:    For uses — all, field, param, return, cast, base, locals.
  head_limit:   Max results to return (default 250). Use with offset to page through large files.
  offset:       Skip first N results before applying head_limit (default 0).

Examples:
  query_single_file("methods", file="$SRC_ROOT/services/Widget.cs")
  query_single_file("calls", "SaveChanges", file="$SRC_ROOT/data/Widget.cs")
  query_single_file("uses", "IRepository", uses_kind="param", file="$SRC_ROOT/services/Widget.cs")
  query_single_file("accesses_on", "IDataStore", file="$SRC_ROOT/services/DataManager.cs")
  query_single_file("methods", file="$SRC_ROOT/Core/BigFile.cs", offset=250)"""
    if not file:
        return "file= is required."

    try:
        _, src_root = _get_root(root)
    except ValueError as e:
        return f"Error: {e}"

    m            = mode.lower().strip().replace("-", "_")
    windows_file = _to_windows_path(file)
    ext          = os.path.splitext(windows_file)[1].lower()

    try:
        with open(windows_file, "rb") as fh:
            src_bytes = fh.read()
    except OSError as e:
        return f"Cannot read file: {e}"

    matches = query_file(
        src_bytes, ext, m, pattern or "",
        include_body=include_body,
        symbol_kind=symbol_kind or None,
        uses_kind=uses_kind or None,
    )

    rel    = _rel_path(_to_windows_path(file), src_root)
    header = f"[{rel}]\n"

    if not matches:
        return header + "No matches found."

    all_lines  = [f"{rel}:{r['line']}: {(r.get('text') or '').rstrip()}" for r in matches]
    total      = len(all_lines)
    page_start = min(offset, total)
    page_end   = min(page_start + head_limit, total)
    out_lines  = all_lines[page_start:page_end]

    page_header = (f"[{page_start + 1}–{page_end} of {total} results]\n\n"
                   if (page_start > 0 or page_end < total) else "")
    output = "\n".join(out_lines)

    output, truncated = _truncate(output)
    if truncated:
        shown   = output.count("\n") + 1
        summary = f"[Result truncated — {len(out_lines)} lines in page. Showing first {shown} lines. Use offset= to page.]\n\n"
        return header + page_header + summary + output
    return header + page_header + output

# ── ready ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def ready(root: str = "") -> str:
    """Check whether the code search index is fully up to date with the file system.

Returns a quick status snapshot (no filesystem walk — returns immediately).
Shows Typesense health, document count, watcher state, queue depth, and last verifier scan.

To trigger a full repair scan use verify_index(action='start'), then poll
ready() or verify_index(action='status') until complete.

Args:
  root: Named source root to check (empty = default root)."""
    try:
        collection, _ = _get_root(root)
    except ValueError as e:
        return f"Error: {e}"

    try:
        status, st = _get("/status")
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
    except Exception as e:
        return f"Indexserver is NOT running: {e}\nStart it with: ts start"

    root_name = root or ("default" if "default" in _ROOTS else next(iter(_ROOTS), ""))
    col_info  = (st.get("collections") or {}).get(root_name, {})
    ndocs     = col_info.get("num_documents")
    lines     = []

    ts_line = ("starting up — retry in a few seconds" if st.get("typesense_loading")
               else "ok" if st.get("typesense_ok") is not False
               else "NOT OK")
    lines.append(f"Typesense  : {ts_line}")
    if st.get("typesense_loading"):
        return "\n".join(lines)

    lines.append(f"Docs       : {ndocs:,}  (collection: {collection})"
                 if ndocs is not None else f"Collection : {collection} — not found")

    watcher = st.get("watcher", {})
    queue   = st.get("queue", {})
    syncer  = st.get("syncer", {})
    w_state = ("running" if watcher.get("running") else
               "paused"  if watcher.get("paused")  else "stopped")
    lines.append(f"Watcher    : {w_state}")
    lines.append(f"Queue      : {queue.get('depth', 0)} pending  ({queue.get('total_queued', 0)} total processed)")

    vp = syncer.get("progress", {})
    if vp:
        vstatus   = vp.get("status", "?")
        missing   = vp.get("missing", 0)
        stale     = vp.get("stale", 0)
        orphaned  = vp.get("orphaned", 0)
        total     = vp.get("total_to_update", 0)
        updated   = vp.get("updated", 0)
        remaining = max(0, total - updated) or (missing + stale)
        lines.append(f"Verifier   : {vstatus}  phase={vp.get('phase', '')}  missing={missing}  "
                     f"stale={stale}  orphaned={orphaned}  updated={updated}/{total}  (last: {vp.get('last_update', '?')})")
        q_depth = queue.get("depth", 0)
        left    = remaining + q_depth
        if vstatus == "complete" and missing == 0 and stale == 0 and orphaned == 0 and q_depth == 0:
            lines.append("Left to index: 0  — index is up to date")
        elif vstatus == "running":
            lines.append(f"Left to index: ~{left}  ({remaining} verifier + {q_depth} queue) — poll again for updates")
        else:
            lines.append("Left to index: unknown — run verify_index(action='start') to check and repair")
    else:
        lines.append("Verifier   : no scan has been run yet")
        lines.append(f"Left to index: {queue.get('depth', 0)} queued — run verify_index(action='start') to check if index is complete")

    return "\n".join(lines)

# ── verify_index ──────────────────────────────────────────────────────────────

@mcp.tool()
def verify_index(
    action: str = "status",
    root: str = "",
    delete_orphans: bool = True,
) -> str:
    """Verify that the code search index is up to date with the file system.

Scans every source file, compares modification times against stored values,
and re-indexes missing or stale files. Orphaned entries are removed unless
delete_orphans=false.

Args:
  action:         "start" | "status" | "stop"
  root:           Named source root to verify (empty = default root).
  delete_orphans: Remove entries for deleted files. Default true."""
    act = action.lower().strip()

    if act == "stop":
        status, data = _post("/verify/stop", {})
        if status == 404:
            return "No verification scan is currently running."
        if status != 200:
            return f"Stop failed ({status}): {data.get('error', data) if isinstance(data, dict) else data}"
        return "Verification scan stopped."

    if act == "status":
        status, data = _get("/status")
        if status != 200:
            return f"Status failed ({status}): {data}"
        syncer = data.get("syncer", {})
        prog   = syncer.get("progress", {})
        if not prog:
            return "No sync has been run yet. Use action='start' to begin."
        running = syncer.get("running", False)
        lines   = []
        if running:
            tot  = prog.get("total_to_update", 0)
            done = prog.get("updated", 0)
            pct  = f"{done * 100 // tot}%" if tot else "—"
            lines.append(f"Running  : yes  ({pct} complete)")
        lines += [
            f"Status   : {prog.get('status', '?')}",
            f"Phase    : {prog.get('phase', '?')}",
            f"Started  : {prog.get('started_at', '?')}",
            f"Updated  : {prog.get('last_update', '?')}",
            f"FS files : {prog.get('fs_files', '?')}",
            f"Index    : {prog.get('index_docs', '?')} docs",
            f"Missing  : {prog.get('missing', 0)}",
            f"Stale    : {prog.get('stale', 0)}",
            f"Orphaned : {prog.get('orphaned', 0)}",
            f"Re-indexed: {prog.get('updated', 0)}",
            f"Deleted  : {prog.get('deleted', 0)} orphans removed",
            f"Errors   : {prog.get('errors', 0)}",
        ]
        return "\n".join(lines)

    if act == "start":
        try:
            collection, src_root = _get_root(root)
        except ValueError as e:
            return f"Error: {e}"
        status, data = _post("/verify/start", {"root": root or "default", "delete_orphans": delete_orphans})
        if status == 409:
            return "A verification scan is already running.\nUse action='status' to monitor, or action='stop' to cancel."
        if status != 200:
            return f"Start failed ({status}): {data.get('error', data) if isinstance(data, dict) else data}"
        return (f"Verification scan started.\n"
                f"Root      : '{root or 'default'}' → {src_root}\n"
                f"Collection: {collection}\n"
                f"Use action='status' to monitor progress.")

    return f"Unknown action: '{action}'. Use 'start', 'status', or 'stop'."

# ── service_status ────────────────────────────────────────────────────────────

@mcp.tool()
def service_status(root: str = "") -> str:
    """Check whether the Typesense code search service is running.
Returns server health, document count per root, and watcher state.
If not running, returns instructions to start it.

Args:
  root: Named root to inspect (empty = show all configured roots)."""
    try:
        status, st = _get("/status", timeout=3)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
    except Exception as e:
        return f"Indexserver is NOT running.\nStart it with: ts start\nError: {e}"

    root_names      = [root] if root else list(_ROOTS)
    indexer_running = (st.get("syncer") or {}).get("running", False)
    ts_line         = ("starting up — retry in a few seconds" if st.get("typesense_loading")
                       else "ok" if st.get("typesense_ok") is not False else "NOT OK")
    lines = [f"Typesense  : {ts_line}"]
    if st.get("typesense_loading"):
        return "\n".join(lines)

    for root_name in root_names:
        try:
            coll_name, _ = _get_root(root_name)
        except ValueError as e:
            lines.append(f"Error: {e}")
            continue
        info   = (st.get("collections") or {}).get(root_name, {})
        ndocs  = info.get("num_documents")
        exists = info.get("collection_exists", ndocs is not None)
        warns  = info.get("schema_warnings") or []
        if not exists:
            lines.append(f"Root '{root_name}' ({coll_name}): " +
                         ("indexing in progress" if indexer_running else "not yet indexed — run: ts index"))
        elif warns:
            lines.append(f"Root '{root_name}' ({coll_name}): {ndocs:,} docs  [SCHEMA OUTDATED — {'; '.join(warns)}]")
        else:
            lines.append(f"Root '{root_name}' ({coll_name}): {ndocs:,} docs")

    return "\n".join(lines)

# ── manage_service ────────────────────────────────────────────────────────────

@mcp.tool()
def manage_service(action: str = "status") -> str:
    """Start, stop, restart, check status, or rebuild the code search service.

Manages the Docker container running the Python indexserver + Typesense.

Args:
  action: One of:
          "start"   — Start the Docker container.
          "stop"    — Stop the Docker container.
          "restart" — Restart the Docker container.
          "status"  — Show service status (document counts, watcher state).
          "rebuild" — Wipe the index and re-index everything from scratch.
                      Runs in the background; monitor with action='status'."""
    VALID = {"start", "stop", "restart", "status", "rebuild"}
    act   = action.lower().strip()
    if act not in VALID:
        return f"Unknown action: '{action}'. Valid: {', '.join(sorted(VALID))}"

    if act == "status":
        try:
            status, data = _get("/status")
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            lines = ["Service status:"]
            for name, info in (data.get("collections") or {}).items():
                ndocs  = info.get("num_documents")
                exists = info.get("collection_exists", True)
                warns  = info.get("schema_warnings") or []
                if not exists:
                    lines.append(f"  Root '{name}': not yet indexed — run manage_service(action='rebuild')")
                elif warns:
                    lines.append(f"  Root '{name}': {ndocs:,} docs  [SCHEMA OUTDATED — {'; '.join(warns)}]")
                else:
                    lines.append(f"  Root '{name}': {ndocs:,} docs  OK")
            w = data.get("watcher", {})
            lines.append(f"Watcher: {w.get('state', 'unknown')}  queue depth: {(data.get('queue') or {}).get('depth', 0)}")
            syncer = data.get("syncer", {})
            if syncer.get("running"):
                lines.append(f"Syncer: running  phase={(syncer.get('progress') or {}).get('phase', '?')}")
            return "\n".join(lines)
        except Exception as e:
            return f"Indexserver not reachable: {e}\nTry: manage_service(action='start')"

    if act == "rebuild":
        results = []
        for root_name in _ROOTS:
            try:
                status, data = _post("/index/start", {"root": root_name, "resethard": True})
                if status == 200:
                    results.append(f"Root '{root_name}': re-indexing started ({data.get('collection', '')})")
                else:
                    err = data.get("error", data) if isinstance(data, dict) else data
                    results.append(f"Root '{root_name}': failed ({status}) — {err}")
            except Exception as e:
                results.append(f"Root '{root_name}': error — {e}")
        results.append("\nRe-indexing is running in the background. Use action='status' to monitor.")
        return "\n".join(results)

    # start / stop / restart — Docker CLI
    res = subprocess.run(["docker", act, _DOCKER],
                         capture_output=True, text=True, timeout=30)
    out = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        return f"docker {act} failed (exit {res.returncode}): {out}"
    return out or f"Service '{act}' completed."

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        from tsquery_server import start_daemon, run_until_shutdown
        if not start_daemon():
            # Another instance already owns the port — nothing to do.
            sys.exit(0)
        run_until_shutdown()
        sys.exit(0)

    # Normal MCP mode: try to start the management server in-process.
    # If the port is already bound (daemon running separately), this is a no-op.
    try:
        from tsquery_server import start_daemon as _start_daemon
        _start_daemon()   # returns False silently if already running
    except Exception:
        pass   # never let a daemon startup error kill the MCP server

    mcp.run()
