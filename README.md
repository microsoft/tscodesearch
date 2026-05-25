# codesearch

Full-text and structural code search for a large monorepo. Runs an in-process [Tantivy](https://github.com/quickwit-oss/tantivy) index (via [tantivy-py](https://github.com/quickwit-oss/tantivy-py)) and exposes results as MCP tools so Claude can query the codebase directly without copy-pasting.

> **Early alpha.** Expect rough edges.

## Installation

```
git clone https://github.com/microsoft/tscodesearch
cd tscodesearch
setup.cmd
ts start
```

`setup.cmd` creates a Python venv (via [uv](https://docs.astral.sh/uv/)), registers the MCP server with Claude Code and VS Code (GitHub Copilot), prompts for a source directory to index, creates `config.json`, and installs the VS Code extension.

To uninstall: `setup.cmd --uninstall`

## Prerequisites

- Windows 11 (or Linux/macOS for the daemon, with caveats)
- Python 3.10+
- Node.js 20+
- `uv` is installed automatically by `setup.mjs` if missing

There is **no Docker, WSL, or Typesense** dependency. The whole index is in-process.

## One-time setup

From a Windows command prompt or PowerShell:

```
setup.cmd
```

`setup.cmd` checks for Node.js then calls `node setup.mjs`, which:
1. Registers the MCP server with Claude Code and VS Code (GitHub Copilot `mcp.servers`)
2. Creates `.client-venv` and installs Python dependencies
3. Creates `config.json` -- prompts for a source directory to index (can be added later)
4. Installs the VS Code extension

After setup, start the daemon:

```
ts start
```

To uninstall: `setup.cmd --uninstall`

## Adding roots

```
ts root --add NAME C:\path\to\source
ts restart
```

Or use the VS Code extension command **TsCodeSearch: Add Root**.

Each root gets its own on-disk Tantivy index at `<repo>/.tantivy/codesearch_NAME/`. Multi-root config in `config.json`:

```json
{
  "api_key": "...",
  "port": 8108,
  "roots": {
    "default": { "path": "C:/myproject/src" },
    "other":   { "path": "D:/other/src" }
  }
}
```

Each root entry can be either an object (`{"path": "...", "extensions": [".cs", ".py"]}`) or a bare string (`"C:/myproject/src"`). The object form is what `setup.mjs` writes and what `ts root --add` produces; the string form is accepted for backwards compatibility.

Use the MCP `root=` parameter to search a specific collection:
```
query_codebase("implements", "IRepository", root="other")
query_single_file("methods", file="path/to/Widget.cs", root="other")
```

## Daemon management

All daemon commands go through `ts.cmd` (Windows CMD/PowerShell):

```
ts status                          show daemon health, doc count, watcher state
ts start                           start the daemon (auto-indexes on first run)
ts stop                            stop the daemon
ts restart                         stop then start
ts verify                          scan FS + repair index: add missing, re-index stale, remove orphans
ts verify --root <name>            verify a specific named root
ts verify --no-delete-orphans      repair without removing deleted-file entries
ts recreate                        stop daemon, wipe the on-disk index, restart (full reindex)
ts recreate --root <name>          recreate a specific named root's index
ts log [-n N]                      tail the daemon log (default: last 40 lines)
```

## Keeping the index up to date

The watcher picks up changes automatically within a couple of seconds (~1 s `ReadDirectoryChangesW` latency + 2 s debounce). For large repos, or after bulk operations like a git pull or branch switch, use the MCP tools or `ts verify` to confirm everything is in sync.

### From Claude (MCP tools)

```
ready()                              # check index readiness (calls /check-ready)
verify_index(action="start")         # launch background sync/repair scan
verify_index(action="status")        # monitor sync progress (reads from GET /status)
verify_index(action="stop")          # cancel a running scan
wait_for_sync(timeout_s=30)          # poll until queue drained; pass 0 for instant status
```

`ready()` returns a summary with `poll_ok` (FS walk completed), `index_ok` (zero missing/stale/orphaned), and timing. If not ready, `verify_index(action="start")` triggers the syncer to repair the index without resetting it.

`wait_for_sync` sleeps up to 1 s (watcher warm-up) then polls `/status` every 0.5 s until the queue is empty. Reports `"Index synced in {N}s"` with a `"was: queue={N}"` note if work was observed, or a timeout message with recovery hints.

### From the command line

```
ts verify                            # foreground repair scan (missing + stale + orphans)
ts verify --no-delete-orphans        # repair without removing deleted-file entries
ts verify --root other               # verify a specific named root
```

## Running tests

```
.client-venv\Scripts\python.exe -m pytest tests/ query/tests/ -v   # full Python suite
node run_tests.mjs                                                 # VS Code extension tests
```

The integration tests open a fresh Tantivy index in `<repo>/.tantivy/test_*` for each class and clean up afterwards. No external service needs to be running.

| File / dir | What it tests |
|------------|---------------|
| `query/tests/` | All language AST query modes against synthetic fixtures |
| `tests/unit/` | Indexer, queue, watcher, verifier, MCP server helpers -- all use `_FakeBackend` |
| `tests/integration/` | Indexer, verifier, watcher, search modes, sample E2E -- open real Tantivy indexes |

## Direct CLI usage

### Management API via curl

```bash
# Read key/port from config.json -- never hard-code
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String(c.port??8108))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

The daemon authenticates every request by matching the `X-API-KEY` header against `config.json`'s `api_key`. The HTTP server binds `localhost` only, but the key still matters: any process on the same machine -- a browser background page, another dev tool, a malicious dependency -- can reach `localhost:PORT`. Requiring a shared secret means a random local process can't query or mutate the index without first reading `config.json`.

### Standalone search CLI (`scripts/search.py`)

```bash
.client-venv\Scripts\python.exe scripts\search.py "BlobStore" --ext cs --limit 5
.client-venv\Scripts\python.exe scripts\search.py "IRepository" --implements
```

This opens the on-disk Tantivy index in read-only mode, so it works whether or not the daemon is running.

### AST queries without a daemon (`python -m query`)

```bash
.client-venv\Scripts\python.exe -m query --mode methods --file C:/myproject/src/Widget.cs
.client-venv\Scripts\python.exe -m query --mode calls   --file C:/myproject/src/Widget.cs --pattern SaveChanges
```

## AST query modes

One canonical mode name per concept across every language. Listing modes take no pattern; pattern modes expect a single identifier (or `LINE:COL` for `at`). Unknown modes raise `ValueError` with the supported-mode list -- use `capabilities` to introspect which modes a given file's language actually supports.

| Mode | Arg | Concept | Languages |
|------|-----|---------|-----------|
| `capabilities` | -- | List the modes supported for this file's language | all |
| `classes` | -- | Type declarations (class/interface/struct/enum/record/...) | all |
| `methods` | -- | Method/ctor/property/field/event declarations | all |
| `fields` | -- | Field / property / column declarations | C#, SQL |
| `imports` | -- | `using` / `import` / `include` directives | all except SQL |
| `params` | METHOD | Parameter list for METHOD | C#, Python, JS, Rust, C++ |
| `declarations` | NAME | Declaration(s) of NAME (narrow with `symbol_kind`) | all |
| `body` | NAME | Full source of NAME's declaration | C# only |
| `at` | LINE:COL | Deepest AST node at position + enclosing scope chain | C# only |
| `calls` | METHOD | Call sites of METHOD (`Repo.Save` restricts by receiver) | all |
| `implements` | TYPE | Types that inherit/implement TYPE | all except SQL |
| `uses` | TYPE | Type references; narrow with `uses_kind` (`field`/`param`/`return`/`cast`/`base`/`locals`) | C# only |
| `casts` | TYPE | `(TYPE)expr` / `as TYPE` sites | C# only |
| `attrs` | NAME? | `[Attribute]` / `@decorator` / `#[attribute]` usages (omit NAME to list all) | C#, Python, JS |
| `accesses_of` | MEMBER | Access sites of property/field by name (`Order.Status` restricts) | C# only |
| `accesses_on` | TYPE | `.Member` accesses on locals/params/fields typed as TYPE | C# only |
| `all_refs` | NAME | Every identifier occurrence (broadest -- AST-only, skips strings/comments). For SQL this is a plain substring scan over lines. | all |

## Architecture

### Two-layer search

1. **Tantivy** -- fast keyword/semantic search over pre-indexed metadata (class names, method names, base types, call sites, signatures, attributes, etc.). Data stored at `<repo>/.tantivy/<collection>/`.

2. **tree-sitter** -- precise AST queries on the file set returned by Tantivy. Skips comments and string literals, understands syntax.

Typical flow: Tantivy narrows the haystack to ~50 candidate files -> tree-sitter parses each one and applies the structural query.

### Process topology

```
,----------------------------------------------------------------,
|  MCP CLIENT  (Claude <-> tools)                                |
|  mcp_server.py  (.client-venv -- runs on Windows)             |
|  Claude Code -> mcp.cmd -> .client-venv\python.exe             |
`------------------------------T-----------------------------------'
                             |  HTTP  localhost:PORT
,-----------------------------v----------------------------------,
|  DAEMON  indexserver/daemon.py  (.client-venv)                 |
|    * HTTP server   (management API on PORT)                  |
|    * watcher       (ReadDirectoryChangesW)                   |
|    * IndexQueue    (batch Tantivy writes)                    |
|    * syncer        (on-demand, via POST /verify/start)       |
|    * Tantivy indexes  (one per root, on disk in .tantivy/)   |
|    * system-tray icon (Windows -- shows Stop menu item)      |
`----------------------------------------------------------------'
```

There is no longer a separate Typesense / Docker / WSL service -- the index lives in-process via `tantivy-py`. On Windows the daemon runs without a console window; right-click the magnifying-glass tray icon to stop it.

### File map

| File | Purpose |
|------|---------|
| `mcp_server.py` | Python MCP server (FastMCP). Tools: `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `wait_for_sync`. |
| `indexserver/daemon.py` | Management daemon. Owns the HTTP API, watcher, IndexQueue, syncer, system-tray icon, and one Tantivy `Backend` per configured root. |
| `mcp.cmd` | Windows launcher: `.client-venv\Scripts\python.exe mcp_server.py` |
| `ts.cmd` / `ts.mjs` | Daemon CLI: start/stop/restart/status/index/verify/log/root |
| `setup.cmd` / `setup.mjs` | One-time setup: `.client-venv`, `config.json`, MCP registration (Claude Code + VS Code), VS Code extension |
| `run_tests.cmd` / `run_tests.mjs` | VS Code extension test runner |

**AST query layer (`query/`)**

| File | Purpose |
|------|---------|
| `query/cs.py`, `py.py`, `js.py`, `rust.py`, `cpp.py`, `sql.py` | Per-language tree-sitter AST functions |
| `query/_util.py` | Shared dataclasses + `TreeIndex` (single-pass AST walker shared by every language) |
| `query/dispatch.py` | Pure query dispatcher. `query_file(src_bytes, ext, mode, pattern, ...)`. No backend dependency. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs` |

`TreeIndex` walks the AST once with tree-sitter's `TreeCursor`, buckets nodes by type, and (optionally) collects literal-aware identifier refs in the same pass. `describe_*_file` covers the union of types every extractor needs in one walk; per-query wrappers (`q_classes`, `q_methods`, ...) pass a narrow type set so they pay the cost of a single targeted walk.

**Indexer (`indexserver/`)**

| File | Purpose |
|------|---------|
| `backend.py` | Tantivy schema + `Backend` class (write/read/upsert/delete/export). |
| `search.py` | Typesense-shaped `search()` on top of `Backend` (multi-field, weights, fuzz, filter_by). |
| `indexer.py` | `walk_source_files()`, `index_file_list()`, `ensure_backend()`, `run_index()`. |
| `verifier.py` | `run_verify()` (two-phase FS diff + repair), `check_ready()`. |
| `watcher.py` | `run_watcher()`. `Observer` on Windows, `PollingObserver` on Linux/WSL. |
| `index_queue.py` | Deduplicated batch queue. Writes go through a `BackendResolver`. |
| `daemon.py` | Management daemon: HTTP server, watcher thread, IndexQueue worker, syncer, tray icon. |
| `query_util.py` | Structural query CLI (`python -m indexserver.query_util ...`). |

**Config**

| File | Purpose |
|------|--------|
| `query/config.py` | `Config`, `Root`, `load_config()`, `collection_for_root()`. |

**Scripts / infra**

| File | Purpose |
|------|---------|
| `scripts/search.py` | Standalone read-only search CLI. |

### Backend schema

Every text field uses Tantivy's `raw` tokenizer: each entry is one verbatim term (case-sensitive, no underscore splitting, no length cap). All domain-aware splitting happens in the indexer before storage -- long identifiers stay whole, `add_text_field` is one token, `Acme.Billing.Service` is three `namespace` entries.

**Indexed search fields** -- populated by the AST extractors and indexed for `query_by` matching; `stored=False`, so values are not retrievable from a search hit:

| Field | Populated from | Used by MCP mode |
|-------|----------------|------------------|
| `base_types` | base classes + interface lists | `implements`, `uses` (`uses_kind=base`) |
| `call_sites` | every call expression's method name | `calls` |
| `field_types` | declared field / property / event types | `uses` (`uses_kind=field`) |
| `param_types` | method / ctor / delegate parameter types | `uses` (`uses_kind=param`) |
| `return_types` | method / delegate return types | `uses` (`uses_kind=return`) |
| `local_types` | declared local variable types | `uses` (`uses_kind=locals`) |
| `cast_types` | `(T)expr`, `as T`, declaration/recursive patterns | `casts`, `uses` (default and `uses_kind=cast`) |
| `type_refs` | union of `field_types` + `param_types` + `return_types` + `base_types` + `local_types` + capitalised call receivers | `uses` (default), `accesses_on` |
| `member_accesses` | RHS of `.Member` access expressions | `accesses_of` |
| `member_sig_tokens` | every identifier in any member signature -- attribute names, parameter names, generic args, default-value identifiers | -- (auxiliary; covers signature content) |
| `attr_names` | `[Attribute]` decorations | `attrs` |
| `imports` | `using`/`import`/`include` modules | `imports` |
| `namespace` | per-component split of the file's primary namespace (e.g. `Acme.Billing.Service` -> 3 entries) | -- (auxiliary) |
| `class_names`, `method_names` | type and method/property/field declarations | `declarations`, `all_refs` |
| `tokens` | deduped bag of every identifier in the file (code only -- no strings or comments) | `all_refs` |
| `path_tokens` | per-directory + filename components -- `services/billing/Foo.cs` -> `["services", "billing", "Foo.cs", "Foo", "cs"]` | every mode (path/filename fallback) |

**Stored fields** -- retrievable from the index at search time:

| Field | Purpose |
|-------|---------|
| `id`, `relative_path` | Document identity, returned with every hit. |
| `filename` | Basename, used for display. |
| `extension`, `language` | Exact-match filters (`extension:=cs`) and status display. |
| `path_segments` | Cumulative ancestor folders for the `sub=` filter (`services/billing/Foo.cs` -> `["services", "services/billing"]`). |
| `mtime` | Verifier diff between filesystem and index. |

Nothing else is stored. The daemon pre-filters with Tantivy then runs tree-sitter on the candidate files; the AST output is what carries line-level results to the caller. Display-only stored payload would just bloat the index.

The daemon resolves `query_by`/`weights` server-side from the mode (and `uses_kind` / `symbol_kind` when relevant); callers don't pass these directly through `/query-codebase`. See `_resolve_query_params` in `tsquery_server.py` for the exact mapping.

### config.json

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": {
    "default": { "path": "C:/myproject/src" }
  }
}
```

This file is **not checked in** (listed in `.gitignore`). It is created by `setup.mjs` with an auto-generated API key. Roots use Windows-style paths (`C:/...`) and are added via `ts root --add` or the VS Code extension. Root entries may also be bare strings -- see *Adding roots* above.
