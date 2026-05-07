"""
Python MCP server for tscodesearch.

Runs on Windows via .client-venv/Scripts/python.exe (stdio transport).

Tools:
  query_codebase     - Typesense pre-filter + tree-sitter AST (via indexserver /query-codebase)
  query_single_file  - Tree-sitter AST on one file (direct import — no indexserver required)
  ready              - Quick index health snapshot
  wait_for_sync      - Block until index has caught up to all pending file events
  verify_index       - Start/stop/monitor index repair scan
  service_status     - Typesense + indexserver status
  manage_service     - Docker container lifecycle (start/stop/restart/rebuild)
"""

import json
import os
import re
import subprocess
import sys
import time
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
# Tier-2 vs tier-3 boundary: when the AST-confirmed match list contains this
# many files or more, we collapse to filenames + hit counts only and direct
# the caller to query_single_file for line-level detail.
_DETAIL_FILES_THRESHOLD = 20
# Tier-3 per-file cap: at most this many `path:line: content` lines per file.
# Files with more than this many AST hits get a query_single_file suggestion.
_PER_FILE_DETAIL_LINES  = 10

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

def _sync_state(data: dict) -> tuple[bool, str]:
    """Inspect a /status response. Returns (is_synced, human_state).

    Synced means: Typesense is up, the index queue is empty, and the syncer
    (verifier walk) is idle with no pending jobs. Watcher activity alone does
    not block — events flow through the queue, which we already check.
    """
    if not isinstance(data, dict):
        return False, "no status response"
    if data.get("typesense_loading"):
        return False, "typesense starting up"
    if data.get("typesense_ok") is False:
        return False, "typesense not healthy"
    queue          = data.get("queue") or {}
    syncer         = data.get("syncer") or {}
    depth          = int(queue.get("depth", 0) or 0)
    syncer_running = bool(syncer.get("running", False))
    syncer_pending = int(syncer.get("pending", 0) or 0)
    if depth == 0 and not syncer_running and syncer_pending == 0:
        return True, "queue empty, syncer idle"
    parts = []
    if depth:
        parts.append(f"queue={depth}")
    if syncer_running:
        parts.append("syncer running")
    if syncer_pending:
        parts.append(f"syncer pending={syncer_pending}")
    return False, ", ".join(parts) or "working"


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
    exclude_path: str = "",
) -> str:
    """Typesense pre-filter + tree-sitter AST. Returns one of three response shapes
depending on result size, picked to keep the response compact and paged-friendly:

  Tier 1 — more than 250 candidate files: a folder drill-down derived from
           Typesense facets (no AST runs at all). Re-issue with a deeper or
           different sub= to narrow.
  Tier 2 — 20-250 files with AST matches: filenames + hit counts only,
           sorted by hits desc. Use query_single_file on a specific file
           to see line-level hits.
  Tier 3 — fewer than 20 files with AST matches: full path:line:content,
           but each file is capped at 10 lines. Files that get capped get
           a per-file query_single_file suggestion appended.

query_single_file accepts the same mode / pattern / root / include_body /
symbol_kind / uses_kind arguments as this tool, so the suggested calls in
tier 2 and tier 3 are drop-in.

For listing modes (methods, fields, classes, usings, imports) use query_single_file.

All modes are identifier-based AST queries. The pattern must be a single
identifier name (e.g. "BlobStore", "SaveChanges") — no whitespace, operators,
punctuation, generic brackets, or quoted strings. Matches are restricted to
identifier occurrences in code; strings and comments are never matched.

If you need a multi-word phrase, an operator-bearing fragment like
"using X =", a literal substring inside a string/comment, or an arbitrary
regex, this tool cannot help — fall back to grep/ripgrep over the source
tree. Do NOT call query_codebase("text", ...) with a multi-word pattern;
it will silently return zero matches.

Args:
  mode:         AST query mode. All take a single identifier as `pattern`.
                C#:     text, declarations, calls, implements, uses, casts,
                        attrs, accesses_of, accesses_on, all_refs
                Python: text, calls, implements, ident, declarations, params,
                        decorators
                text is an alias for all_refs — every identifier occurrence of
                the given name. Use it when you don't yet know which structural
                role (call vs declaration vs cast vs param type) you're after.
                Prefer a more specific mode (calls, declarations, uses, etc.)
                when you do.
  pattern:      A single identifier. Examples that DO work: "BlobStore",
                "SaveChanges", "IDataStore". Examples that do NOT work:
                "using BlobStore", "(BlobStore)", "Save Changes",
                "List<Foo>", "// TODO". Use grep for those.
  sub:          Narrow to an ancestor folder. Accepts any depth, e.g.
                "services" or "services/billing". Comma-separated values
                form a logical OR: sub="services,vendor" searches files
                under either tree. On overflow the response suggests
                deeper paths to drill into.
  ext:          File extension filter. Common values: "cs", "py", "cpp".
                For C/C++, "cpp" automatically includes header files (.h, .hpp, .hxx).
                Omit to search all indexed languages. Default: cs.
  context_lines: Surrounding source lines per match.
  root:         Named source root (empty = default).
  include_body: For declarations — include full body. Default false.
  symbol_kind:  For declarations — restrict to: method, class, interface, etc.
  uses_kind:    For uses — all, field, param, return, cast, base, locals.
  exclude_path: Comma-separated list of folder paths to exclude from results.
                Each value is matched as an exact ancestor folder, not a glob —
                wildcards are not supported. Behavior:
                  - "tests"                    excludes any file under any tests/
                                               directory at any depth
                                               (e.g. tests/, src/tests/, a/b/tests/)
                  - "services/billing/legacy"  excludes only that exact subtree
                  - "tests,generated,vendor"   excludes all three (logical OR)
                Composes with sub= as set intersection: scope to one tree, then
                exclude subtrees within it. Backslashes are normalised to "/" and
                leading/trailing slashes are stripped, so paths from any OS work.

Examples:
  query_codebase("calls", "SaveChanges", sub="services")
  query_codebase("uses", "IDataStore", uses_kind="param", sub="services")
  query_codebase("implements", "IRepository")
  query_codebase("declarations", "SaveChanges", symbol_kind="method")
  query_codebase("calls", "SaveChanges", sub="services,vendor")
  query_codebase("calls", "SaveChanges", exclude_path="tests,generated")
  query_codebase("uses", "IRepo", sub="services", exclude_path="services/legacy")"""
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
            "exclude_path": exclude_path or "",
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
        scopes = [s.strip("/") for s in (sub or "").replace("\\", "/").split(",")]
        scopes = [s for s in scopes if s]

        counts: list[tuple[str, int]] = []
        seen_vals: set[str] = set()
        if scopes:
            for scope in scopes:
                scope_depth = scope.count("/") + 1
                next_depth  = scope_depth + 1
                prefix      = scope + "/"
                for fc in facets:
                    if fc.get("field_name") != "path_segments":
                        continue
                    for c in fc.get("counts", []):
                        val = c["value"]
                        if val in seen_vals:
                            continue
                        if not val.startswith(prefix):
                            continue
                        if (val.count("/") + 1) != next_depth:
                            continue
                        seen_vals.add(val)
                        counts.append((val, int(c["count"])))
        else:
            for fc in facets:
                if fc.get("field_name") != "path_segments":
                    continue
                for c in fc.get("counts", []):
                    val = c["value"]
                    if val in seen_vals or "/" in val:
                        continue
                    seen_vals.add(val)
                    counts.append((val, int(c["count"])))

        lines = [f"Too many files ({found}) — narrowing required.",
                 "Repeat with a deeper sub= to scope further, then re-run.", ""]
        if counts:
            counts.sort(key=lambda x: -x[1])
            scope_label = f" under '{','.join(scopes)}'" if scopes else ""
            lines.append(f"Folders{scope_label} with '{pattern}' hits — re-run with sub=<path>:")
            for name, count in counts[:25]:
                lines.append(f'  query_codebase("{m}", "{pattern}", sub="{name}")  # ~{count} files')
        else:
            lines.append("No deeper folder breakdown available — try a more specific pattern.")
        return warn + "\n".join(lines)

    # AST-confirmed files only — drop Typesense false positives.
    files_with_matches: list[tuple[str, list]] = []
    total_matches = 0
    for hit in hits:
        matches = hit.get("matches") or []
        if not matches:
            continue
        rel = (hit.get("document") or {}).get("relative_path", "")
        files_with_matches.append((rel, matches))
        total_matches += len(matches)

    n_files = len(files_with_matches)
    header  = (f"[Typesense: {found} files | files with matches: {n_files} | "
               f"total matches: {total_matches}]\n")
    if not files_with_matches:
        return warn + header + "No AST matches found."

    files_with_matches.sort(key=lambda fm: -len(fm[1]))

    def _qsf_call(file_rel: str) -> str:
        """A query_single_file call mirroring the current query_codebase params."""
        args = [f'"{m}"']
        if pattern:
            args.append(f'"{pattern}"')
        args.append(f'file="$SRC_ROOT/{file_rel}"')
        if root:
            args.append(f'root="{root}"')
        if include_body:
            args.append("include_body=True")
        if symbol_kind:
            args.append(f'symbol_kind="{symbol_kind}"')
        if uses_kind:
            args.append(f'uses_kind="{uses_kind}"')
        return "query_single_file(" + ", ".join(args) + ")"

    # Tier 2 — many files: filenames + counts only.
    if n_files >= _DETAIL_FILES_THRESHOLD:
        body_lines = [
            f"{rel}  ({len(matches)} hit{'s' if len(matches) != 1 else ''})"
            for rel, matches in files_with_matches
        ]
        suggestion = (f"\n\n{n_files} files matched — line-level results omitted. "
                      f"To see hits in a specific file:\n"
                      f"  {_qsf_call(files_with_matches[0][0])}")
        output = "\n".join(body_lines) + suggestion
        output, truncated = _truncate(output)
        if truncated:
            shown = output.count("\n") + 1
            note  = f"[Result truncated — showing first {shown} lines of {n_files}.]\n\n"
            return warn + header + note + output
        return warn + header + output

    # Tier 3 — few files: full content, but cap each file at PER_FILE_DETAIL_LINES.
    out_lines: list[str] = []
    truncated_files: list[tuple[str, int]] = []
    for rel, matches in files_with_matches:
        for match in matches[:_PER_FILE_DETAIL_LINES]:
            out_lines.append(f"{rel}:{match['line']}: {(match.get('text') or '').rstrip()}")
        if len(matches) > _PER_FILE_DETAIL_LINES:
            truncated_files.append((rel, len(matches)))

    output = "\n".join(out_lines)
    if truncated_files:
        notes = [f"\n\n{len(truncated_files)} file(s) had more than "
                 f"{_PER_FILE_DETAIL_LINES} hits — showing first "
                 f"{_PER_FILE_DETAIL_LINES} of each. To see all hits in a file:"]
        for rel, total in truncated_files:
            notes.append(f"  {_qsf_call(rel)}  # {total} total hits")
        output += "\n".join(notes)

    output, truncated = _truncate(output)
    if truncated:
        shown   = output.count("\n") + 1
        summary = f"[Result truncated — showing first {shown} lines.]\n\n"
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

Pattern modes are identifier-based: `pattern` must be a single identifier
name (e.g. "BlobStore"), not a phrase, regex, or punctuation-bearing fragment.
Matches are restricted to identifier occurrences in code — strings and comments
are not matched. For literal substring search, multi-word phrases, operators,
or comment fragments, this tool cannot help — use grep/ripgrep on the file.

Args:
  mode:    AST query mode.
           C# pattern-required: uses, calls, implements, casts, declarations,
             attrs, accesses_of, accesses_on, all_refs, text, params
           C# listing (no pattern): methods, fields, classes, usings
           Python pattern-required: calls, implements, ident, declarations, decorators, text, params
           Python listing (no pattern): classes, methods, imports
           text is an alias for all_refs — every identifier occurrence of the
             pattern. Use it as a fallback when no other mode fits; prefer the
             specific mode (calls, declarations, uses, etc.) when you know the
             role you're after. Do NOT pass a multi-word pattern to text — it
             will return zero matches; reach for grep instead.
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

# ── wait_for_sync ─────────────────────────────────────────────────────────────

@mcp.tool()
def wait_for_sync(timeout_s: float = 30.0, root: str = "") -> str:
    """Block until the index has caught up to all pending file events.

Use this between editing files and querying the index, to make sure your
recent edits are reflected in query_codebase results. Without it, results
can be a second or two stale on Windows (watcher latency + queue drain).

Polls the indexserver's /status endpoint every 0.5 s, and returns once
Typesense is healthy AND the index queue is empty AND the syncer is idle.
A small initial delay (~1 s) is built in so events from a just-completed
edit have time to reach the watcher before the first poll.

Args:
  timeout_s: Maximum seconds to wait. Default 30. The indexer typically
             catches up in well under a second when only a few files
             changed; raise this for large rewrites or initial indexing.
  root:      Named source root (empty = default). Currently informational —
             the indexserver tracks queue/syncer state globally, not per
             root, so this argument is reserved for future use.

Returns:
  On success: "Index synced in {N}s" (plus a brief description of what
  was pending when polling began, if anything).
  On timeout: a state line describing what is still in flight.
  On error:   a connection error message with a hint to start the daemon.
"""
    try:
        if root:
            _get_root(root)
    except ValueError as e:
        return f"Error: {e}"

    start         = time.monotonic()
    deadline      = start + max(0.0, float(timeout_s))
    initial_delay = min(1.0, max(0.0, float(timeout_s)))
    poll_interval = 0.5
    last_state    = "unknown"
    initial_state = None

    time.sleep(initial_delay)

    while True:
        try:
            # /status touches Typesense to fetch live doc counts — under load
            # that round-trip can spike past a couple of seconds, so use a
            # generous per-call timeout and retry transient failures rather
            # than bailing on the first hiccup.
            status, data = _get("/status", timeout=10)
        except Exception as e:
            if time.monotonic() >= deadline:
                return (f"Indexserver is unreachable after {timeout_s:.0f}s: {e}\n"
                        f"If it should be running, check service_status() or run: ts start")
            remaining = deadline - time.monotonic()
            time.sleep(min(poll_interval, max(0.0, remaining)))
            continue

        if status != 200:
            return f"Status check failed: HTTP {status}"

        synced, state = _sync_state(data if isinstance(data, dict) else {})
        if initial_state is None:
            initial_state = state
        last_state = state
        if synced:
            elapsed = time.monotonic() - start
            if initial_state and initial_state != state:
                return f"Index synced in {elapsed:.1f}s (was: {initial_state})"
            return f"Index synced in {elapsed:.1f}s"

        if time.monotonic() >= deadline:
            elapsed = time.monotonic() - start
            return (f"Timed out after {elapsed:.1f}s — still working: {last_state}.\n"
                    f"Re-run wait_for_sync with a larger timeout_s, or run "
                    f"verify_index(action='start') if the index looks stuck.")

        remaining = deadline - time.monotonic()
        time.sleep(min(poll_interval, max(0.0, remaining)))

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
