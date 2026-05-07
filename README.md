# codesearch

Full-text and structural code search for a large monorepo. Runs an in-process [Tantivy](https://github.com/quickwit-oss/tantivy) index (via [tantivy-py](https://github.com/quickwit-oss/tantivy-py)) and exposes results as MCP tools so Claude can query the codebase directly without copy-pasting.

> **Early alpha.** Expect rough edges. The only supported install path is cloning the repository and running `setup.cmd`.

## Installation

```
git clone https://github.com/microsoft/tscodesearch
cd tscodesearch
setup.cmd
```

`setup.cmd` creates a Python venv (via [uv](https://docs.astral.sh/uv/)) with all dependencies including `tantivy`, registers the MCP server with Claude Code, creates `config.json`, and installs the VS Code extension. After it completes:

```
ts start
```

Then open VS Code (or reload: **Ctrl+Shift+P > Reload Window**) and use **TsCodeSearch: Add Root** to point at your source directory.

## Prerequisites

- Windows 11 (or Linux/macOS for the daemon, with caveats)
- Node.js 20+
- `uv` is installed automatically by `setup.mjs` if missing

There is **no Docker, WSL, or Typesense** dependency anymore. The whole index is in-process.

## One-time setup

From a Windows command prompt or PowerShell:

```
setup.cmd
```

`setup.cmd` checks for Node.js then calls `node setup.mjs`, which:
1. Registers the MCP server with Claude Code
2. Creates `.client-venv` and installs Python dependencies
3. Creates `config.json` with an auto-generated API key
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
    "default": "C:/myproject/src",
    "other":   "D:/other/src"
  }
}
```

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
ts index                           re-index in background (incremental)
ts index --resethard               wipe the on-disk index and reindex from scratch
ts index --root <name>             index a specific named root
ts verify                          scan FS + repair index: add missing, re-index stale, remove orphans
ts verify --root <name>            verify a specific named root
ts verify --no-delete-orphans      repair without removing deleted-file entries
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
wait_for_sync(timeout_s=30)          # block until queue is drained
```

`ready()` returns a summary with `poll_ok` (FS walk completed), `index_ok` (zero missing/stale/orphaned), and timing. If not ready, `verify_index(action="start")` triggers the syncer to repair the index without resetting it.

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
| `tests/unit/` | Indexer, queue, watcher, verifier, MCP server helpers — all use `_FakeBackend` |
| `tests/integration/` | Indexer, verifier, watcher, search modes, sample E2E — open real Tantivy indexes |

## Direct CLI usage

### Management API via curl

```bash
# Read key/port from config.json — never hard-code
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String(c.port??8108))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

The `X-TYPESENSE-API-KEY` header name is preserved for backwards compatibility with existing callers; the daemon just checks that any value matches `config.json`'s `api_key`.

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

## Architecture

### Two-layer search

1. **Tantivy** — fast keyword/semantic search over pre-indexed metadata (class names, method names, base types, call sites, signatures, attributes, etc.). Data stored at `<repo>/.tantivy/<collection>/`.

2. **tree-sitter** — precise AST queries on the file set returned by Tantivy. Skips comments and string literals, understands syntax.

Typical flow: Tantivy narrows the haystack to ~50 candidate files → tree-sitter parses each one and applies the structural query.

### Process topology

```
┌──────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                                │
│  mcp_server.py  (.client-venv — runs on Windows)             │
│  Claude Code → mcp.cmd → .client-venv\python.exe             │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP  localhost:PORT
┌────────────────────────────▼─────────────────────────────────┐
│  DAEMON  tsquery_server.py  (.client-venv — runs on Windows) │
│    • HTTP server   (management API on PORT)                  │
│    • watcher       (ReadDirectoryChangesW)                   │
│    • IndexQueue    (batch Tantivy writes)                    │
│    • syncer        (on-demand, via POST /index/start)        │
│    • Tantivy indexes  (one per root, on disk in .tantivy/)   │
└──────────────────────────────────────────────────────────────┘
```

There is no longer a separate Typesense / Docker / WSL service — the index lives in-process via `tantivy-py`.

### File map

| File | Purpose |
|------|---------|
| `mcp_server.py` | Python MCP server (FastMCP). Tools: `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `wait_for_sync`. |
| `tsquery_server.py` | Management daemon. Owns the HTTP API, watcher, IndexQueue, syncer, and one Tantivy `Backend` per configured root. |
| `mcp.cmd` | Windows launcher: `.client-venv\Scripts\python.exe mcp_server.py` |
| `ts.cmd` / `ts.mjs` | Daemon CLI: start/stop/restart/status/index/verify/log/root |
| `setup.cmd` / `setup.mjs` | One-time setup: `.client-venv`, `config.json`, MCP registration, VS Code extension |
| `run_tests.cmd` / `run_tests.mjs` | VS Code extension test runner |

**AST query layer (`query/`)**

| File | Purpose |
|------|---------|
| `query/cs.py`, `py.py`, `js.py`, `rust.py`, `cpp.py`, `sql.py` | Per-language tree-sitter AST functions |
| `query/dispatch.py` | Pure query dispatcher. `query_file(src_bytes, ext, mode, pattern, ...)`. No backend dependency. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs` |

**Indexer (`indexserver/`)**

| File | Purpose |
|------|---------|
| `backend.py` | Tantivy schema + `Backend` class (write/read/upsert/delete/export). |
| `search.py` | Typesense-shaped `search()` on top of `Backend` (multi-field, weights, fuzz, filter_by). |
| `config.py` | `Config`, `Root`, `load_config()`. |
| `indexer.py` | `walk_source_files()`, `index_file_list()`, `ensure_backend()`, `run_index()`. |
| `verifier.py` | `run_verify()` (two-phase FS diff + repair), `check_ready()`. |
| `watcher.py` | `run_watcher()`. `Observer` on Windows, `PollingObserver` on Linux/WSL. |
| `index_queue.py` | Deduplicated batch queue. Writes go through a `BackendResolver`. |
| `query_util.py` | Structural query CLI (`python -m indexserver.query_util ...`). |

**Scripts / infra**

| File | Purpose |
|------|---------|
| `scripts/search.py` | Standalone read-only search CLI. |

### Backend schema

The `Backend` schema includes one stored, default-tokenized text field per pre-extracted symbol kind:

| Tier | Fields | Used by MCP mode |
|------|--------|-----------------|
| T1 | `base_types` | `implements` |
| T1 | `call_sites` | `calls` |
| T1 | `member_sigs` | `declarations` |
| T2 | `type_refs` | `uses` |
| T2 | `attr_names` | `attrs` |
| T2 | `usings` | — |
| — | `class_names`, `method_names`, `tokens` | `text` |
| — | `filename`, `relative_path` | every mode |

The `path_segments` field is the list of every ancestor folder of each file (e.g. `services/billing/Foo.cs` → `["services", "services/billing"]`). Use `sub=` to scope searches to any folder path — single segment (`services`) or nested (`services/billing`). On overflow, the response suggests deeper folder paths to drill into.

The default Tantivy tokenizer splits on whitespace + ASCII punctuation, so a value like `Task<Widget>` is automatically searchable as both `task` and `widget` — no token-separator configuration is needed.

### config.json

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": {
    "default": "C:/myproject/src"
  }
}
```

This file is **not checked in** (listed in `.gitignore`). It is created by `setup.mjs` with an auto-generated API key. Roots use Windows-style paths (`C:/...`) and are added via `ts root --add` or the VS Code extension.
