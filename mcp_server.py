"""
Python MCP server for tscodesearch.

Runs on Windows via .client-venv/Scripts/python.exe (stdio transport).

Tools:
  query_codebase     - Tantivy pre-filter + tree-sitter AST (via daemon /query-codebase)
  query_single_file  - Tree-sitter AST on one file (direct import -- no daemon required)
  ready              - Quick index health snapshot
  wait_for_sync      - Block until index has caught up to all pending file events
  service_status     - Daemon status
"""

import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).parent
sys.path.insert(0, str(_REPO))

from mcp.server.fastmcp import FastMCP
from query.dispatch import query_file
from query.config import Root, load_config, normalize_path

# -- Config --------------------------------------------------------------------

_cfg      = load_config()
_API_PORT = _cfg.port
_API_KEY  = _cfg.api_key
_ROOTS    = _cfg.roots  # dict[str, Root]

_MAX_OUTPUT_CHARS     = 40_000
_QUERY_CODEBASE_LIMIT = 250
# Tier-2 vs tier-3 boundary: when the AST-confirmed match list contains this
# many files or more, we collapse to filenames + hit counts only and direct
# the caller to query_single_file for line-level detail.
_DETAIL_FILES_THRESHOLD = 20
# Tier-3 per-file cap: at most this many `path:line: content` lines per file.
# Files with more than this many AST hits get a query_single_file suggestion.
_PER_FILE_DETAIL_LINES  = 10

# -- HTTP helpers --------------------------------------------------------------

def _http(method: str, path: str, body=None, timeout: int = 120):
    url     = f"http://localhost:{_API_PORT}{path}"
    headers = {"X-API-KEY": _API_KEY}
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

# -- Config helpers ------------------------------------------------------------

def _resolve_root(name: str) -> Root:
    """Resolve a root name to a ``Root`` object.

    Empty name resolves to ``"default"`` or the first configured root. Raises
    ``ValueError`` with a clear message when no roots are configured at all,
    or when the requested name isn't known.
    """
    if not _ROOTS:
        raise ValueError("No roots configured. Run: ts root --add NAME PATH")
    try:
        return _cfg.get_root(name)
    except ValueError:
        available = ", ".join(sorted(_ROOTS))
        raise ValueError(f"Unknown root '{name}'. Available: {available}")

def _to_windows_path(file_path: str) -> str:
    """Resolve a tool-input file path to an absolute Windows-style path.

    Accepts an absolute drive path (``C:/...``) or a path relative to the
    default root (with optional ``${SRC_ROOT}`` / ``$SRC_ROOT`` prefix).
    """
    default_root = ""
    if _ROOTS:
        default = _ROOTS.get("default") or next(iter(_ROOTS.values()))
        default_root = normalize_path(default.path).rstrip("/")

    p = normalize_path(file_path)
    p = p.replace("${SRC_ROOT}", default_root).replace("$SRC_ROOT", default_root)

    if re.match(r"^[A-Za-z]:", p):
        return p

    if default_root:
        return f"{default_root}/{p}"

    return p

def _rel_path(file_path: str, src_root: str) -> str:
    norm = normalize_path(file_path)
    root = normalize_path(src_root).rstrip("/")
    if norm.lower().startswith(root.lower() + "/"):
        return norm[len(root) + 1:]
    return norm

def _queue_warning() -> str:
    try:
        status, data = _get("/status", timeout=2)
        if status != 200 or not isinstance(data, dict):
            return ""
        depth = data.get("queue", {}).get("depth", 0)
        if depth > 0:
            return f"[WARNING: index has outstanding work -- {depth} files queued. Results may be incomplete.]\n\n"
    except Exception:
        pass
    return ""

def _sync_state(data: dict) -> tuple[bool, str]:
    """Inspect a /status response. Returns (is_synced, human_state).

    Synced means the index queue is empty.
    """
    if not isinstance(data, dict):
        return False, "no status response"
    queue = data.get("queue") or {}
    depth = int(queue.get("depth", 0) or 0)
    if depth == 0:
        return True, "queue empty"
    return False, f"queue={depth}"


def _truncate(output: str) -> tuple[str, bool]:
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output, False
    trunc = output[:_MAX_OUTPUT_CHARS]
    nl    = trunc.rfind("\n")
    return (trunc[:nl] if nl > 0 else trunc), True

# -- MCP server ----------------------------------------------------------------

mcp = FastMCP("tscodesearch")

# -- query_codebase ------------------------------------------------------------

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
    visibility: str = "",
    head_lines: int = 0,
    enclosing_method: str = "",
    enclosing_class: str = "",
    exclude_path: str = "",
) -> str:
    """Index pre-filter + tree-sitter AST. Returns one of three response shapes
depending on result size, picked to keep the response compact and paged-friendly:

  Tier 1 -- more than 250 candidate files: a folder drill-down derived from
           index facets (no AST runs at all). Re-issue with a deeper or
           different sub= to narrow.
  Tier 2 -- 20-250 files with AST matches: filenames + hit counts only,
           sorted by hits desc. Use query_single_file on a specific file
           to see line-level hits.
  Tier 3 -- fewer than 20 files with AST matches: full path:line:content,
           but each file is capped at 10 lines. Files that get capped get
           a per-file query_single_file suggestion appended.

query_single_file accepts the same mode / pattern / root / include_body /
symbol_kind / uses_kind arguments as this tool, so the suggested calls in
tier 2 and tier 3 are drop-in.

For listing modes (methods, fields, classes, imports, capabilities) use query_single_file.

All modes are identifier-based AST queries. The pattern must be a single
identifier name (e.g. "BlobStore", "SaveChanges") -- no whitespace, operators,
punctuation, generic brackets, or quoted strings. Matches are restricted to
identifier occurrences in code; strings and comments are never matched.

If you need a multi-word phrase, an operator-bearing fragment, a literal
substring inside a string/comment, or an arbitrary regex, this tool cannot
help -- fall back to grep/ripgrep over the source tree.

Args:
  mode:         AST query mode. All take a single identifier as `pattern`.
                C#:     declarations, calls, implements, uses, casts,
                        attrs, accesses_of, accesses_on, all_refs
                Python: calls, implements, declarations, decorators, all_refs
                Use all_refs when you don't yet know which structural role
                (call vs declaration vs cast vs param type) you're after.
                Prefer a more specific mode (calls, declarations, uses, etc.)
                when you do.
                Each mode expects a specific *kind* of identifier in `pattern`,
                and silently returns empty if you pass the wrong kind:
                  - `calls` wants a METHOD name (e.g. "SaveChanges"). Passing a
                    variable/receiver name (e.g. "blobStore") matches almost
                    nothing -- it only fires if that name is invoked directly
                    as `blobStore(...)`, not on `blobStore.Method()` calls.
                    To find every usage of a variable, use `all_refs` on the
                    variable name.
                  - `accesses_on` wants a TYPE name (e.g. "IRepository"). It
                    finds `.Member`/`?.Member` accesses on locals/params/fields
                    of that type, plus `new T { Prop = ... }` initializers and
                    `with` mutations. It returns NOTHING when the variable is
                    only assigned, returned, or forwarded as an argument (no
                    `.Member` exists). When `accesses_on` is empty but you know
                    the variable exists, fall back to `all_refs` on the
                    variable name.
                  - `accesses_of` wants a MEMBER name (e.g. "Timeout"). It
                    only finds *qualified* reads (`expr.Timeout`). Bare
                    identifier reads in the declaring class itself -- which
                    compile to implicit `this.Timeout` -- are NOT matched.
                    For implicit-this reads use `all_refs` on the member
                    name.
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
  include_body: For declarations -- include full body. Default false.
  symbol_kind:  For declarations -- restrict to: method, class, interface, etc.
  uses_kind:    For `uses` -- narrow to one structural role. Values:
                  - omitted / "all" (default): union of `type_refs` +
                    `cast_types` -- every type reference anywhere in the file.
                  - field, param, return, cast, base, locals: narrow to that
                    one role.
  visibility:   For declaration modes (declarations / classes / methods /
                fields) -- comma-separated access modifiers to keep. Values:
                public, internal, protected, private. Empty = no filter.
                Languages that don't capture visibility (e.g. SQL) match
                nothing when this filter is set. (C# captures explicit
                modifiers plus interface-public / enum-public / nested
                type defaults.)
  head_lines:   For `body` and `declarations include_body=True` -- truncate
                each emitted body to the first N source lines (signature +
                body together), with a `... +K more lines` tail marker.
                Default 0 = no truncation. Useful when scanning many bodies
                at once.
  enclosing_method:
                For pattern modes (calls / uses / casts / accesses_of /
                accesses_on / all_refs) -- only keep hits inside a member
                with this exact name. Combine with `enclosing_class=` to
                pinpoint a specific call-site context. (C# only.)
  enclosing_class:
                Same as above, narrowed to a type by name. Composes with
                `enclosing_method=` as a logical AND. (C# only.)
  exclude_path: Comma-separated list of folder paths to exclude from results.
                Each value is matched as an exact ancestor folder, not a glob --
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
  query_codebase("body", "SaveChanges", symbol_kind="method")  # full source of every match
  query_codebase("var_type", "store", sub="services")  # resolved type of every ``store`` occurrence
  query_codebase("calls", "SaveChanges", sub="services,vendor")
  query_codebase("calls", "SaveChanges", exclude_path="tests,generated")
  query_codebase("uses", "IRepo", sub="services", exclude_path="services/legacy")"""
    # File-targeted modes don't make sense for a codebase-wide search.
    # `at`/`params` need an explicit file; listing modes
    # (methods/fields/classes/imports/capabilities) only describe a single
    # file's structure. Catch them here so the agent sees an actionable
    # redirect instead of the daemon's generic "unknown mode" error.
    # ``body`` and ``var_type`` work codebase-wide now (index pre-filter
    # + per-file AST), so they're no longer in this set.
    _FILE_ONLY = {
        "methods", "fields", "classes", "imports", "capabilities",
        "at", "params",
    }
    m = mode.lower().strip().replace("-", "_")
    if m in _FILE_ONLY:
        if m in ("at", "params"):
            why = "needs a specific file"
            example_arg = pattern or ('"SaveChanges"' if m != "at" else '"42:10"')
            example = f'  query_single_file("{m}", {example_arg}, file="path/to/File.cs")'
        else:
            why = "lists one file's contents without filtering"
            example = f'  query_single_file("{m}", file="path/to/File.cs")'
        return (f"Mode '{m}' {why} -- use query_single_file instead:\n{example}")

    try:
        status, data = _post("/query-codebase", {
            "mode": m, "pattern": pattern, "sub": sub or "",
            "ext": (ext or "").lstrip("."),
            "root": root or "", "limit": _QUERY_CODEBASE_LIMIT,
            "include_body": include_body,
            "symbol_kind": symbol_kind or "", "uses_kind": uses_kind or "",
            "visibility": visibility or "",
            "head_lines": int(head_lines) if head_lines else 0,
            "enclosing_method": enclosing_method or "",
            "enclosing_class": enclosing_class or "",
            "exclude_path": exclude_path or "",
        })
    except Exception as e:
        return f"Could not reach indexserver: {e}\nStart it with: ts start"

    warn = _queue_warning()

    if status == 503 and isinstance(data, dict) and data.get("loading"):
        return "Daemon is still starting up -- retry in a few seconds.\nUse service_status() to check when it is ready."
    if status >= 400:
        err    = data.get("error", json.dumps(data)) if isinstance(data, dict) else str(data)
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        msg    = f"TSCODESEARCH ERROR -- do not fall back to Grep/Glob; investigate and fix.\nError from indexserver: {err}"
        if detail:
            msg += f"\nDetail: {detail}"
        return warn + msg

    found  = data.get("found", 0)
    hits   = data.get("hits", [])
    facets = data.get("facet_counts", [])

    if data.get("overflow"):
        scopes = [s.strip("/") for s in normalize_path(sub or "").split(",")]
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

        lines = [f"Too many files ({found}) -- narrowing required.",
                 "Repeat with a deeper sub= to scope further, then re-run.", ""]
        if counts:
            counts.sort(key=lambda x: -x[1])
            scope_label = f" under '{','.join(scopes)}'" if scopes else ""
            lines.append(f"Folders{scope_label} with '{pattern}' hits -- re-run with sub=<path>:")
            for name, count in counts[:25]:
                lines.append(f'  query_codebase("{m}", "{pattern}", sub="{name}")  # ~{count} files')
        else:
            lines.append("No deeper folder breakdown available -- try a more specific pattern.")
        return warn + "\n".join(lines)

    # AST-confirmed files only -- drop index false positives.
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
    header  = (f"[Index: {found} files | files with matches: {n_files} | "
               f"total matches: {total_matches}]\n")
    if not files_with_matches:
        return warn + header + "No AST matches found."

    files_with_matches.sort(key=lambda fm: -len(fm[1]))

    def _qsf_call(file_rel: str) -> str:
        """A query_single_file call mirroring the current query_codebase params.

        Uses the bare relative path from the tier-2/3 listing -- the tool
        accepts that directly (it prepends the default root), so injecting
        a ``$SRC_ROOT/`` placeholder just adds noise.
        """
        args = [f'"{m}"']
        if pattern:
            args.append(f'"{pattern}"')
        args.append(f'file="{file_rel}"')
        if root:
            args.append(f'root="{root}"')
        if include_body:
            args.append("include_body=True")
        if symbol_kind:
            args.append(f'symbol_kind="{symbol_kind}"')
        if uses_kind:
            args.append(f'uses_kind="{uses_kind}"')
        if visibility:
            args.append(f'visibility="{visibility}"')
        if head_lines:
            args.append(f'head_lines={int(head_lines)}')
        if enclosing_method:
            args.append(f'enclosing_method="{enclosing_method}"')
        if enclosing_class:
            args.append(f'enclosing_class="{enclosing_class}"')
        return "query_single_file(" + ", ".join(args) + ")"

    # Tier 2 -- many files: filenames + counts only.
    if n_files >= _DETAIL_FILES_THRESHOLD:
        body_lines = [
            f"{rel}  ({len(matches)} hit{'s' if len(matches) != 1 else ''})"
            for rel, matches in files_with_matches
        ]
        suggestion = (f"\n\n{n_files} files matched -- line-level results omitted. "
                      f"To see hits in a specific file:\n"
                      f"  {_qsf_call(files_with_matches[0][0])}")
        output = "\n".join(body_lines) + suggestion
        output, truncated = _truncate(output)
        if truncated:
            shown = output.count("\n") + 1
            note  = f"[Result truncated -- showing first {shown} lines of {n_files}.]\n\n"
            return warn + header + note + output
        return warn + header + output

    # Tier 3 -- few files: full content, but cap each file at PER_FILE_DETAIL_LINES.
    out_lines: list[str] = []
    truncated_files: list[tuple[str, int]] = []
    for rel, matches in files_with_matches:
        for match in matches[:_PER_FILE_DETAIL_LINES]:
            out_lines.append(f"{rel}:{match['line']}: {(match.get('text') or '').rstrip()}")
        if len(matches) > _PER_FILE_DETAIL_LINES:
            truncated_files.append((rel, len(matches)))

    output = "\n".join(out_lines)
    if truncated_files:
        # Compact form: one short line per capped file showing how many
        # extra hits are available. The agent already knows the mode and
        # pattern; spelling out the full ``query_single_file(...)`` call
        # for every capped file would burn ~100 chars per file in pure
        # boilerplate. ``[+K capped] path`` is enough -- the agent can
        # reissue a query_single_file call when it wants the rest.
        notes = [f"\n\n{len(truncated_files)} file(s) capped at "
                 f"{_PER_FILE_DETAIL_LINES} hits each. Issue "
                 f"query_single_file(\"{m}\", \"{pattern}\", file=...) "
                 f"to see all hits."]
        for rel, total in truncated_files:
            extra = total - _PER_FILE_DETAIL_LINES
            notes.append(f"  [+{extra} capped] {rel}")
        output += "\n".join(notes)

    output, truncated = _truncate(output)
    if truncated:
        shown   = output.count("\n") + 1
        summary = f"[Result truncated -- showing first {shown} lines.]\n\n"
        return warn + header + summary + output
    return warn + header + output

# -- query_single_file ---------------------------------------------------------

@mcp.tool()
def query_single_file(
    mode: str,
    pattern: str = "",
    file: str = "",
    root: str = "",
    include_body: bool = False,
    symbol_kind: str = "",
    uses_kind: str = "",
    visibility: str = "",
    head_lines: int = 0,
    enclosing_method: str = "",
    enclosing_class: str = "",
    head_limit: int = 250,
    offset: int = 0,
) -> str:
    """Run a tree-sitter AST query on a single file. No index search.

Mode names are canonical -- one name per concept across every language.
Call `query_single_file("capabilities", file=...)` first if you're not sure
which modes a given file's language supports.

Pattern modes are identifier-based: `pattern` must be a single identifier
name (e.g. "BlobStore"), not a phrase, regex, or punctuation-bearing fragment.
Matches are restricted to identifier occurrences in code -- strings and comments
are not matched. For literal substring search, multi-word phrases, operators,
or comment fragments, this tool cannot help -- use grep/ripgrep on the file.

Modes (canonical, same name across languages):

  Listing modes -- omit `pattern`:
    classes       Type declarations (class, interface, struct, enum, record, ...).
    methods       Method, constructor, property, field, event declarations.
    fields        Field and property declarations (C#, SQL).
    imports       What this file pulls in (using/import/include directives).
    capabilities  List the modes actually supported for this file's language.

  Pattern modes -- `pattern` is a single identifier:
    declarations NAME   The declaration(s) of NAME (filter with `symbol_kind`).
    body NAME           Full source of NAME's declaration (include_body=true).
    calls METHOD        Call sites of METHOD. Pass a METHOD name, not a
                        receiver -- `obj.Foo()` is matched by calls("Foo")
                        not calls("obj"); for variable usage use all_refs.
    caller_of METHOD    Like ``calls``, but groups results by the enclosing
                        caller -- one row per ``(TypeName.MemberName)``
                        with a count of how many call sites it contains.
                        (C# only.)
    callee_of METHOD    The inverse: walk the body of the method named
                        METHOD and emit one row per distinct callee with
                        invocation counts. Constructor calls show as
                        ``T (N invocations, ctor)``. (C# only.)
    implements TYPE     Types that inherit/implement TYPE.
    uses TYPE           Type references. Omit `uses_kind` (or "all") for
                        the union of every role; narrow with `uses_kind`
                         in  {field, param, return, cast, base, locals}.
                        (C# only.)
    casts TYPE          Explicit (TYPE)expr / as TYPE sites.
    attrs NAME?         [Attribute] / @decorator usages (omit NAME to list all).
    params METHOD       Parameters of METHOD.
    accesses_of MEMBER  Qualified access sites of property/field MEMBER --
                        `expr.MEMBER`. Bare `MEMBER` (implicit `this.MEMBER`
                        inside the declaring class) is NOT matched; use
                        `all_refs MEMBER` for that. (C# only.)
    accesses_on TYPE    .Member accesses on locals/params declared as TYPE.
                        Returns NOTHING when the variable is only assigned,
                        returned, or forwarded as an argument -- no `.Member`
                        exists. Fall back to all_refs on the variable then.
                        (C# only.)
    all_refs NAME       Every identifier occurrence (broadest; AST-only,
                        skips strings/comments).
    var_type NAME       For each occurrence of NAME, report its resolved
                        type from the method-scoped var-type map (or
                        `(unresolved)` / `(conflicting)`). Use this to
                        answer "what's the type of `foo` at line 42"
                        without having to find the exact column for `at`.
                        (C# only today.)

  Position mode -- `pattern` is "LINE:COL" (1-indexed):
    at LINE:COL         Identify the deepest AST node at the position and
                        print the chain of enclosing named declarations
                        with their line ranges. Use for stack traces, test
                        failures, or review comments that point at a
                        file:line[:col]. (C# only today.)

Args:
  mode:         One of the modes above.
  pattern:      Identifier (most modes), "LINE:COL" (`at` mode), or omitted
                (listing modes).
  file:         Absolute path. Windows paths (C:/...) or $SRC_ROOT-prefixed
                paths. Relative paths are NOT supported.
  root:         Named source root (empty = default).
  include_body: For `declarations` -- include full body. Default false. (Use
                the `body` mode instead for one-shot member-source retrieval.)
  symbol_kind:  For `declarations` / `body` -- restrict to method, ctor, class,
                interface, struct, enum, record, delegate, property, field,
                event, type, or member.
  uses_kind:    For `uses` -- narrow to one structural role. Omit (or pass
                "all") for the union of every role; otherwise one of
                field, param, return, cast, base, locals. (C# only.)
  visibility:   For declaration modes (declarations / classes / methods /
                fields) -- comma-separated access modifiers to keep
                (public, internal, protected, private). Omit for no
                filter. C# captures explicit modifiers and applies the
                language's defaults (interface members => public, nested
                types => private, top-level types => internal); other
                languages currently return nothing when this filter is set.
  head_lines:   For `body` and `declarations include_body=True` -- truncate
                each body to the first N source lines (signature + body
                together) with a `... +K more lines` tail marker.
                Default 0 = no truncation.
  enclosing_method / enclosing_class:
                For pattern modes -- restrict hits to those inside a
                member / type with the given name. Composes with both
                filters as a logical AND. (C# only.)
  head_limit:   Max results to return (default 250). Use with offset to page.
  offset:       Skip first N results before applying head_limit (default 0).

Errors:
  Unknown mode or unsupported-for-this-language -> returns an error line that
  lists the modes that ARE supported. Use `capabilities` to enumerate them
  programmatically before calling.

Examples (relative paths resolve against the default root; ``$SRC_ROOT/``
prefix is still accepted for back-compat but no longer required):
  query_single_file("capabilities", file="services/Widget.cs")
  query_single_file("methods",      file="services/Widget.cs")
  query_single_file("body",      "SaveChanges", file="data/Widget.cs")
  query_single_file("at",        "42:10",       file="data/Widget.cs")
  query_single_file("calls",     "SaveChanges", file="data/Widget.cs")
  query_single_file("var_type",  "store",       file="data/Widget.cs")
  query_single_file("uses",      "IRepository", uses_kind="param",
                    file="services/Widget.cs")"""
    if not file:
        return "file= is required."

    try:
        src_root = _resolve_root(root).path
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

    try:
        matches = query_file(
            src_bytes, ext, m, pattern or "",
            include_body=include_body,
            symbol_kind=symbol_kind or None,
            uses_kind=uses_kind or None,
            visibility=visibility or None,
            head_lines=int(head_lines) if head_lines else None,
            enclosing_method=enclosing_method or None,
            enclosing_class=enclosing_class or None,
        )
    except ValueError as e:
        # Unknown extension or unsupported mode -- propagate the helpful
        # message instead of returning an empty result, so the agent learns
        # which modes ARE supported for this file.
        return f"Error: {e}"

    rel    = _rel_path(_to_windows_path(file), src_root)
    header = f"[{rel}]\n"

    if not matches:
        return header + "No matches found."

    def _fmt_line(r: dict) -> str:
        # Listing modes (methods/classes/fields) include end_line for scope ranges
        # so callers can Read the body precisely. Pattern modes emit point
        # matches without end_line.
        text = (r.get("text") or "").rstrip()
        end  = r.get("end_line")
        loc  = f"{r['line']}-{end}" if end else f"{r['line']}"
        return f"{rel}:{loc}: {text}"

    all_lines  = [_fmt_line(r) for r in matches]
    total      = len(all_lines)
    page_start = min(offset, total)
    page_end   = min(page_start + head_limit, total)
    out_lines  = all_lines[page_start:page_end]

    page_header = (f"[{page_start + 1}-{page_end} of {total} results]\n\n"
                   if (page_start > 0 or page_end < total) else "")
    output = "\n".join(out_lines)

    output, truncated = _truncate(output)
    if truncated:
        shown   = output.count("\n") + 1
        summary = f"[Result truncated -- {len(out_lines)} lines in page. Showing first {shown} lines. Use offset= to page.]\n\n"
        return header + page_header + summary + output
    return header + page_header + output

# -- ready ---------------------------------------------------------------------

@mcp.tool()
def ready(root: str = "") -> str:
    """Check whether the code search index is fully up to date with the file system.

Returns a quick status snapshot (no filesystem walk -- returns immediately).
Shows daemon health, document count, watcher state, and queue depth.

Args:
  root: Named source root to check (empty = default root)."""
    try:
        r = _resolve_root(root)
    except ValueError as e:
        return f"Error: {e}"
    collection = r.collection
    root_name  = r.name

    try:
        status, st = _get("/status")
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
    except Exception as e:
        return f"Indexserver is NOT running: {e}\nStart it with: ts start"

    col_info = (st.get("collections") or {}).get(root_name, {})
    ndocs     = col_info.get("num_documents")
    lines     = []

    lines.append(f"Docs       : {ndocs:,}  (collection: {collection})"
                 if ndocs is not None else f"Collection : {collection} -- not found")

    watcher = st.get("watcher", {})
    queue   = st.get("queue", {})
    w_state = ("running" if watcher.get("running") else
               "paused"  if watcher.get("paused")  else "stopped")
    lines.append(f"Watcher    : {w_state}")
    lines.append(f"Queue      : {queue.get('depth', 0)} pending  ({queue.get('total_queued', 0)} total processed)")

    return "\n".join(lines)

# -- wait_for_sync -------------------------------------------------------------

@mcp.tool()
def wait_for_sync(timeout_s: float = 30.0, root: str = "") -> str:
    """Block until the index has caught up to all pending file events.

Use this between editing files and querying the index, to make sure your
recent edits are reflected in query_codebase results. Without it, results
can be a second or two stale on Windows (watcher latency + queue drain).

Polls the daemon /status endpoint every 0.5 s until the index queue is
empty. A warm-up delay of up to 1 s is applied first so file-system events
from a just-completed edit have time to reach the watcher before the first
poll. Pass timeout_s=0 to skip the warm-up and get an immediate snapshot
of the current queue depth.

Args:
  timeout_s: Maximum seconds to wait. Default 30. The indexer typically
             catches up in under a second when only a few files changed;
             raise this for large rewrites or initial indexing.
             Pass 0 for a non-blocking status check.
  root:      Named source root (empty = default). Currently informational --
             the daemon tracks queue state globally, not per root.

Returns:
  On success: "Index synced in {N}s" (plus "was: {state}" if work was
  observed during polling).
  On timeout: description of what is still in flight, with recovery hints.
  On error:   connection error with a hint to start the daemon.
"""
    try:
        if root:
            _resolve_root(root)
    except ValueError as e:
        return f"Error: {e}"

    start         = time.monotonic()
    deadline      = start + max(0.0, float(timeout_s))
    # Warm-up: capped at 1 s; automatically 0 when timeout_s=0 (instant check).
    warmup        = min(1.0, max(0.0, float(timeout_s)))
    poll_interval = 0.5
    first_busy:   str | None = None  # first non-empty state seen, for "was:" message

    # Let in-flight file-system events reach the watcher before the first poll.
    if warmup > 0:
        time.sleep(min(warmup, max(0.0, deadline - time.monotonic())))

    while True:
        try:
            # /status can spike under load; use a generous per-call timeout and
            # retry transient failures rather than bailing on the first hiccup.
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
        if not synced and first_busy is None:
            first_busy = state

        if synced:
            elapsed = time.monotonic() - start
            if first_busy is not None:
                return f"Index synced in {elapsed:.1f}s (was: {first_busy})"
            return f"Index synced in {elapsed:.1f}s"

        if time.monotonic() >= deadline:
            elapsed = time.monotonic() - start
            return (f"Timed out after {elapsed:.1f}s -- still working: {state}.\n"
                    f"Re-run wait_for_sync with a larger timeout_s, or restart the daemon if the index looks stuck.")

        remaining = deadline - time.monotonic()
        time.sleep(min(poll_interval, max(0.0, remaining)))

# -- service_status ------------------------------------------------------------

@mcp.tool()
def service_status(root: str = "") -> str:
    """Check whether the code search daemon is running.
Returns daemon health, document count per root, and watcher state.
If not running, returns instructions to start it.

Args:
  root: Named root to inspect (empty = show all configured roots)."""
    try:
        status, st = _get("/status", timeout=3)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
    except Exception as e:
        return f"Daemon is NOT running.\nStart it with: ts start\nError: {e}"

    root_names = [root] if root else list(_ROOTS)
    lines = []

    for root_name in root_names:
        try:
            coll_name = _resolve_root(root_name).collection
        except ValueError as e:
            lines.append(f"Error: {e}")
            continue
        info  = (st.get("collections") or {}).get(root_name, {})
        ndocs = info.get("num_documents")
        exists = info.get("collection_exists", ndocs is not None)
        if not exists:
            lines.append(f"Root '{root_name}' ({coll_name}): not yet indexed")
        else:
            lines.append(f"Root '{root_name}' ({coll_name}): {ndocs:,} docs")

    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def _ensure_daemon() -> None:
    """Spawn the daemon and wait for it to be healthy.

    Spawns unconditionally -- the daemon exits immediately if the port is
    already bound (another instance won the race).  The port binding inside
    start_daemon() is the mutual-exclusion primitive.

    The daemon is headless (pystray tray icon only) and is launched detached
    so it survives after the MCP server exits.
    """
    import subprocess

    # Always use the venv Python so indexserver + tantivy are importable,
    # regardless of which Python is running the MCP server.
    # On Windows, read pyvenv.cfg to find the base Python's real pythonw.exe
    # (GUI subsystem -- never creates a console).  The venv shim pythonw.exe is
    # a uv launcher that internally re-spawns python.exe (console subsystem).
    _scripts = _REPO / ".client-venv" / "Scripts"
    _pyvenv  = _REPO / ".client-venv" / "pyvenv.cfg"
    py = None
    extra_env: dict = {}
    if sys.platform == "win32" and _pyvenv.exists():
        for line in _pyvenv.read_text("utf-8").splitlines():
            if line.lower().startswith("home"):
                _, _, home = line.partition("=")
                _base_pythonw = Path(home.strip()) / "pythonw.exe"
                if _base_pythonw.exists():
                    py = str(_base_pythonw)
                    _venv = _REPO / ".client-venv"
                    extra_env = {
                        "VIRTUAL_ENV": str(_venv),
                        "PYTHONPATH":  str(_venv / "Lib" / "site-packages"),
                        "PATH": str(_scripts) + os.pathsep + os.environ.get("PATH", ""),
                    }
                break
    if py is None:
        if sys.platform == "win32":
            _pythonw = _scripts / "pythonw.exe"
            py = str(_pythonw) if _pythonw.exists() else str(_scripts / "python.exe")
        else:
            _pybin = _REPO / ".client-venv" / "bin" / "python"
            py = str(_pybin) if _pybin.exists() else sys.executable

    kwargs: dict = dict(
        cwd=str(_REPO),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **extra_env},
    )
    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    subprocess.Popen([py, "-m", "indexserver.daemon"], **kwargs)

    # Wait for the daemon (ours or the winner of a concurrent race) to be healthy.
    # Use an event so SIGTERM/SIGINT aborts the wait immediately. Signal handlers
    # are restored before returning so FastMCP's own handlers are unaffected.
    exit_event = threading.Event()
    def _on_exit(sig, frame): exit_event.set()
    old_sigterm = signal.signal(signal.SIGTERM, _on_exit)
    try:
        old_sigint = signal.signal(signal.SIGINT, _on_exit)
    except (OSError, ValueError):
        old_sigint = None

    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not exit_event.is_set():
            try:
                _get("/health", timeout=2)
                return
            except Exception:
                exit_event.wait(timeout=0.5)
        if not exit_event.is_set():
            print("[mcp_server] WARNING: daemon did not become healthy within 20s", flush=True)
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    _ensure_daemon()
    mcp.run()
