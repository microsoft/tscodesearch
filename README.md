# codesearch

Full-text and structural code search for a large monorepo. Runs a [Typesense](https://typesense.org) search server and exposes results as MCP tools so Claude can query the codebase directly without copy-pasting.

> **Early alpha.** Expect rough edges. The only supported install path is cloning the repository and running `setup.cmd`.

## Installation

```
git clone https://github.com/microsoft/tscodesearch
cd tscodesearch
setup.cmd          # Docker mode (default)
setup.cmd --wsl    # WSL mode (alternative)
```

`setup.cmd` builds the MCP server, registers it with Claude Code, creates `config.json`, and installs the VS Code extension. After it completes:

```
ts start
```

Then open VS Code (or reload: **Ctrl+Shift+P > Reload Window**) and use **TsCodeSearch: Add Root** to point at your source directory.

## Prerequisites

**Docker mode (default):**
- Docker Desktop installed and running
- Node.js 20+

**WSL mode (alternative):**
- Windows 11 with WSL2
- Python 3.10+ available in WSL (`python3 --version`)
- Node.js 20+

## One-time setup

From a Windows command prompt or PowerShell:

```
setup.cmd          # Docker mode (default)
setup.cmd --wsl    # WSL mode
```

`setup.cmd` checks for Node.js then calls `node setup.mjs`, which:
1. Builds the MCP server (`npm install && npm run build`)
2. Registers the MCP server with Claude Code
3. Sets up the WSL environment (WSL mode only)
4. Creates `config.json` with an auto-generated API key and `mode` field
5. Installs the VS Code extension

After setup, start the service:

```
ts start
```

Then open VS Code (or reload: **Ctrl+Shift+P > Reload Window**) and use **TsCodeSearch: Add Root** to point at your source directory.

To uninstall: `setup.cmd --uninstall`

## Adding roots

```
ts root --add NAME C:\path\to\source
ts restart
```

Or use the VS Code extension command **TsCodeSearch: Add Root**.

Each root gets its own Typesense collection (`codesearch_NAME`). Multi-root config in `config.json`:

```json
{
  "api_key": "...",
  "mode": "docker",
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

## Service management

All service commands go through `ts.cmd` (Windows CMD/PowerShell):

```
ts status                          show server health, doc count, watcher/heartbeat state
ts start                           start the service (auto-indexes if needed)
ts stop                            stop everything
ts restart                         stop then start
ts index                           re-index in background (incremental, keeps existing collection)
ts index --resethard               wipe all data and reindex from scratch
ts index --root <name>             index a specific named root
ts verify                          scan FS + repair index: add missing, re-index stale, remove orphans
ts verify --root <name>            verify a specific named root
ts verify --no-delete-orphans      repair without removing deleted-file entries
ts log                             tail the service log
ts log --indexer [-n N]            tail the indexer/verifier log (default: last 40 lines)
```

The same `ts` commands work for both Docker and WSL modes — `ts.mjs` reads the `mode` field from `config.json` to decide which backend to use.

## Keeping the index up to date

The watcher picks up changes automatically within ~12 seconds (10 s poll interval + 2 s debounce). For large repos, or after bulk operations like a git pull or branch switch, use the MCP tools or `ts verify` to confirm everything is in sync.

### From Claude (MCP tools)

```
ready()                              # check index readiness (calls /check-ready on indexserver)
verify_index(action="start")         # launch background sync/repair scan
verify_index(action="status")        # monitor sync progress (reads from GET /status)
verify_index(action="stop")          # cancel a running scan
```

`ready()` returns a summary with `poll_ok` (FS walk completed), `index_ok` (zero missing/stale/orphaned), and timing. If not ready, `verify_index(action="start")` triggers the syncer to repair the index without resetting it.

### From the command line

```
ts verify                            # foreground repair scan (missing + stale + orphans)
ts verify --no-delete-orphans        # repair without removing deleted-file entries
ts verify --root other               # verify a specific named root
```

## Running tests

Three modes, all via `run_tests.mjs` (or the `run_tests.cmd` wrapper):

```
node run_tests.mjs --docker                    # Docker mode (default)
node run_tests.mjs --wsl                       # WSL mode (isolated, non-destructive)
node run_tests.mjs --linux                     # Linux/CI
```

Filter by test name, class, or file:
```
node run_tests.mjs --wsl -k TestVerifier
node run_tests.mjs --docker tests/test_query_cs.py
```

The full suite is ~899 tests. Docker E2E mounts `tests/` as a volume — test changes don't require rebuilding the image.

### Structural query tests (no server needed)

74 tests covering all 15 `query_cs` modes against a synthetic C# fixture:

```
node run_tests.mjs --wsl tests/test_query_cs.py
```

| File | What it tests |
|------|---------------|
| `test_query_cs.py` | All C# AST query modes against `tests/query_fixture.cs` |
| `test_indexer.py` | Indexer, semantic fields, multi-root, `extract_cs_metadata`, `index_file_list` pipeline |
| `test_indexer_query_consistency.py` | Cross-checks that indexer and query extract the same values from identical source |
| `test_watcher.py` | File watcher event handler (unit + integration) |
| `test_process_cs.py` | `process_cs_file()` C# structural query API |
| `test_python.py` | Python metadata extraction (`extract_py_metadata`), `process_py_file()`, Python semantic fields |
| `test_verifier.py` | `_export_index()` (mock HTTP), `run_verify()` diff logic, full sync integration |

## Direct CLI usage

### Management API via curl

```bash
# Read key/port from config.json — never hard-code
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String((c.port??8108)+1))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

### AST queries without a server (`python -m query`)

```bash
# Runs on Windows via .client-venv — no indexserver needed
.client-venv\Scripts\python.exe -m query --mode methods --file C:/myproject/src/Widget.cs
.client-venv\Scripts\python.exe -m query --mode calls   --file C:/myproject/src/Widget.cs --pattern SaveChanges
```

## Architecture

### Two-layer search

1. **Typesense** — fast keyword/semantic search over pre-indexed metadata (class names, method names, base types, call sites, signatures, attributes, etc.). Data stored at `~/.local/typesense/` (WSL) or in a Docker volume.

2. **tree-sitter** — precise C# AST queries on the file set returned by Typesense. Skips comments and string literals, understands syntax.

Typical flow: Typesense narrows the haystack to ~50 candidate files → tree-sitter parses each one and applies the structural query.

### Process topology

**Docker mode (default):**

```
┌──────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                                │
│  mcp_server.py  (.client-venv — runs on Windows)             │
│  Claude Code → mcp.cmd → .client-venv\python.exe            │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP  localhost:PORT+1
┌────────────────────────────▼─────────────────────────────────┐
│  DAEMON  tsquery_server.py  (.client-venv — runs on Windows) │
│    • HTTP server   (management API on PORT+1)                │
│    • watcher       (ReadDirectoryChangesW)                   │
│    • IndexQueue    (batch Typesense writes)                  │
│    • syncer        (on-demand, via POST /index/start)        │
│    • heartbeat     (Typesense health check + auto-restart)   │
└──────────────────────────────────────────────────────────────┘
                             │  TCP  localhost:PORT
                        Typesense server
                      (Docker container — volume for data)
```

**WSL mode:**

```
┌──────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                                │
│  mcp_server.py  (.client-venv — runs on Windows)             │
│  Claude Code → mcp.cmd → .client-venv\python.exe            │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP  localhost:PORT+1
┌────────────────────────────▼─────────────────────────────────┐
│  DAEMON  tsquery_server.py  (.client-venv — runs on Windows) │
│    • HTTP server   (management API on PORT+1)                │
│    • watcher       (ReadDirectoryChangesW on Windows)        │
│    • IndexQueue    (batch Typesense writes)                  │
│    • syncer        (on-demand, via POST /index/start)        │
│    • heartbeat     (Typesense health check + auto-restart)   │
└──────────────────────────────────────────────────────────────┘
                             │  TCP  localhost:PORT (WSL2 auto-forwards)
                        Typesense server
                      (WSL Linux binary — data at ~/.local/typesense/)
```

### File map

**Client-side (repo root)**

| File | Purpose |
|------|---------|
| `mcp_server.py` | Python MCP server (FastMCP). Exposes `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `manage_service`. Calls `tsquery_server.start_daemon()` at startup. Runs under `.client-venv` on Windows. |
| `tsquery_server.py` | Management daemon. Owns the HTTP API on PORT+1, watcher, IndexQueue, syncer, and heartbeat threads. Runs under `.client-venv` on Windows. |
| `mcp.cmd` | Windows launcher: `.client-venv\Scripts\python.exe mcp_server.py` |
| `ts.cmd` | Thin wrapper: `node ts.mjs %*` |
| `ts.mjs` | Management CLI: start/stop/restart/status/index/verify/log/root/build/setup. Reads `mode` from `config.json`. Calls `entrypoint.sh` directly for Typesense lifecycle in WSL mode. |
| `setup.cmd` | Thin wrapper: checks Node.js 20+, calls `node setup.mjs %*` |
| `setup.mjs` | One-time setup: `.client-venv`, WSL venv, `config.json`, MCP registration, VS Code extension. |
| `run_tests.cmd` | Thin wrapper: `node run_tests.mjs %*` |
| `run_tests.mjs` | Test runner: `--docker`, `--wsl`, or `--linux` mode |

**AST query layer (`query/`)**

| File | Purpose |
|------|---------|
| `query/cs.py` | C# AST functions + `query_cs_bytes()` |
| `query/py.py` | Python AST functions + `query_py_bytes()` |
| `query/js.py` | JS/TS AST functions + `query_js_bytes()` |
| `query/rust.py` | Rust AST functions + `query_rust_bytes()` |
| `query/cpp.py` | C/C++ AST functions + `query_cpp_bytes()` |
| `query/sql.py` | SQL AST functions + `query_sql_bytes()` |
| `query/dispatch.py` | Pure query dispatcher. `query_file(src_bytes, ext, mode, pattern, ...)`. No Typesense dependency. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs` |

**Server-side (`indexserver/`)**

| File | Purpose |
|------|---------|
| `config.py` | Reads `config.json`. `Config`, `Root`, `load_config()`, `to_native_path()`. |
| `indexer.py` | `walk_source_files()`, `index_file_list()`, `build_schema()`, `ensure_collection()`, `export_index_map()`. |
| `verifier.py` | `run_verify()` (two-phase FS diff + repair), `check_ready()` (read-only health check). |
| `watcher.py` | `run_watcher()`. `Observer` on Windows (ReadDirectoryChangesW), `PollingObserver` on Linux/WSL. |
| `index_queue.py` | Deduplicated batch queue for all Typesense writes. |
| `start_server.py` | Downloads Typesense binary (`--install`). |

**Scripts / infra**

| File | Purpose |
|------|---------|
| `scripts/entrypoint.sh` | Full Typesense lifecycle for Docker and WSL. WSL: `--background --disown` start, `--stop` stop, `--background --disown --resethard` wipe+restart, `--log` tail logs. Docker: foreground mode (no flags). |
| `scripts/wsl-setup.sh` | WSL environment setup (venv, Typesense binary) — called by `setup.mjs --wsl` |
| `docker/Dockerfile` | Docker image definition |
| `docker/docker-compose.yml` | Docker Compose configuration |

### Typesense schema

The collection uses tiered semantic fields extracted by tree-sitter at index time:

| Tier | Fields | Used by MCP mode |
|------|--------|-----------------|
| T1 | `base_types` | `implements` |
| T1 | `call_sites` | `calls` |
| T1 | `method_sigs` | `declarations` |
| T2 | `type_refs` | `uses` |
| T2 | `attributes` | `attrs` |
| T2 | `usings` | — |
| — | `class_names`, `method_names`, `symbols` | `text` |
| — | `content` | `text` |

Search ranking by file type: `.cs` (priority 3) → `.h/.cpp/.c` (2) → scripts/`.py/.ts` (1) → config/docs (0).

The `path_segments` field is the list of every ancestor folder of each file (e.g. `services/billing/Foo.cs` → `["services", "services/billing"]`). Use `sub=` to scope searches to any folder path — single segment (`services`) or nested (`services/billing`). On overflow, the response suggests deeper folder paths to drill into.

### config.json

```json
{
  "api_key": "codesearch-local",
  "mode": "docker",
  "roots": {
    "default": "C:/myproject/src"
  }
}
```

This file is **not checked in** (listed in `.gitignore`). It is created by `setup.mjs` with an auto-generated API key. The `mode` field is `"docker"` (default) or `"wsl"`. Roots use Windows-style paths (`C:/...`) and are added via `ts root --add` or the VS Code extension.
