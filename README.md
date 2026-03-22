# codesearch

Full-text and structural code search for a large monorepo. Runs a [Typesense](https://typesense.org) search server and exposes results as MCP tools so Claude can query the codebase directly without copy-pasting.

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

`setup.cmd` checks for Node.js then calls `node setup.mjs`, which does everything:
1. Builds the MCP server (`npm install && npm run build`)
2. Registers the MCP server with Claude Code
3. Sets up the WSL environment (WSL mode only)
4. Creates `config.json` with an auto-generated API key and `mode` field
5. Starts the service
6. Installs the VS Code extension

After setup, open VS Code and use **TsCodeSearch: Add Root** to point at your source directory.

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
ts index --reset                   drop + recreate collection, then re-index
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
ready()                              # poll FS + check index is in sync (synchronous, ~30 s for 200k files)
verify_index(action="start")         # launch background repair scan
verify_index(action="status")        # monitor repair progress
verify_index(action="stop")          # cancel a running scan
```

`ready()` returns a summary with `poll_ok` (FS walk completed), `index_ok` (zero missing/stale/orphaned), and timing. If not ready, `verify_index(action="start")` repairs the index without resetting it.

### From the command line

```
ts verify                            # foreground repair scan (missing + stale + orphans)
ts verify --no-delete-orphans        # repair without removing deleted-file entries
ts verify --root other               # verify a specific named root
```

## Running tests

Three modes, all via `run_tests.mjs` (or the `run_tests.cmd` wrapper):

```
node run_tests.mjs --docker                            # Docker mode (default)
node run_tests.mjs --wsl --destructive                 # WSL mode (erases + resets WSL index)
node run_tests.mjs --linux                             # Linux/CI
```

Filter by test name, class, or file:
```
node run_tests.mjs --wsl --destructive -k TestVerifier
node run_tests.mjs --docker tests/test_query_cs.py
```

The full suite is ~697 tests. Docker E2E mounts `tests/` as a volume — test changes don't require rebuilding the image.

### Structural query tests (no server needed)

74 tests covering all 15 `query_cs` modes against a synthetic C# fixture:

```
node run_tests.mjs --wsl --destructive tests/test_query_cs.py
```

| File | What it tests |
|------|---------------|
| `test_query_cs.py` | All C# AST query modes against `tests/query_fixture.cs` |
| `test_indexer.py` | Indexer, semantic fields, multi-root, `extract_cs_metadata`, `index_file_list` pipeline |
| `test_indexer_query_consistency.py` | Cross-checks that indexer and query extract the same values from identical source |
| `test_watcher.py` | File watcher event handler (unit + integration) |
| `test_process_cs.py` | `process_file()` C# structural query API |
| `test_python.py` | Python metadata extraction (`extract_py_metadata`), `process_py_file()`, Python semantic fields |
| `test_verifier.py` | `_export_index()` (mock HTTP), `run_verify()` diff logic, full verify integration |

## Direct CLI usage

### Full-text search (`search.py`)

```bash
# From WSL:
~/.local/indexserver-venv/bin/python search.py "Widget"
~/.local/indexserver-venv/bin/python search.py "ProcessOrder" --ext cs --sub payments
~/.local/indexserver-venv/bin/python search.py "IRepository"  --mode implements
~/.local/indexserver-venv/bin/python search.py "SaveChanges"  --mode calls
~/.local/indexserver-venv/bin/python search.py "Obsolete"     --mode attrs
~/.local/indexserver-venv/bin/python search.py "ConnectionString" --mode uses
```

### Structural C# AST queries (`query.py`)

```bash
# Listing modes (no pattern needed)
~/.local/indexserver-venv/bin/python query.py --methods  Order.cs
~/.local/indexserver-venv/bin/python query.py --classes  Order.cs
~/.local/indexserver-venv/bin/python query.py --fields   Order.cs
~/.local/indexserver-venv/bin/python query.py --usings   Order.cs

# Pattern modes with explicit file(s) or glob
~/.local/indexserver-venv/bin/python query.py --calls     SaveChanges        "src/data/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --calls     Repository.Save    "src/data/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --casts     Widget             "src/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --all-refs  ProcessOrder       "src/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --accesses-on Widget           Order.cs
~/.local/indexserver-venv/bin/python query.py --accesses-of Status           "src/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --attrs     TestMethod         "src/**/*.cs"
~/.local/indexserver-venv/bin/python query.py --declarations ProcessOrder    Order.cs
~/.local/indexserver-venv/bin/python query.py --params    SaveChanges        Order.cs

# Pattern modes with --search (Typesense finds the files automatically)
~/.local/indexserver-venv/bin/python query.py --implements IRepository       --search "IRepository"
~/.local/indexserver-venv/bin/python query.py --uses       Order             --search "Order"
~/.local/indexserver-venv/bin/python query.py --uses       ConnectionString  --uses-kind field   --search "ConnectionString"
~/.local/indexserver-venv/bin/python query.py --uses       Widget            --uses-kind param   --search "Widget"
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
│  mcp_server.js  (Node.js — runs on Windows)                  │
│  Claude Code → mcp.cmd → node mcp_server.js                  │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP  localhost:PORT+1
                             │  (indexserver management API)
┌────────────────────────────▼─────────────────────────────────┐
│  DOCKER CONTAINER                                            │
│  indexserver/api.py  (management API + thread manager)       │
│    • watcher thread    (PollingObserver)                      │
│    • heartbeat thread  (Typesense health check)              │
│    • verifier thread   (on-demand, via POST /verify)         │
│  /app/tests (volume mount)                                   │
└──────────────────────────────────────────────────────────────┘
                             │  internal
                        Typesense server
                      (Docker volume for data)
```

**WSL mode:**

```
┌──────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                                │
│  mcp_server.js  (Node.js — runs on Windows)                  │
│  Claude Code → mcp.cmd → node mcp_server.js                  │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP  localhost:PORT+1
┌────────────────────────────▼─────────────────────────────────┐
│  INDEXSERVER  (WSL process: api.py)                          │
│    • watcher thread    (PollingObserver, /mnt/)              │
│    • heartbeat thread  (Typesense health check)              │
│    • verifier thread   (on-demand, via POST /verify)         │
│  Venv: ~/.local/indexserver-venv/                            │
└──────────────────────────────────────────────────────────────┘
                             │  data at ~/.local/typesense/
                        Typesense server (Linux binary)
```

> **MCP server is Node.js.** `mcp.cmd` (Windows) runs `node mcp_server.js`; on Linux/WSL run it directly — no Python venv needed for the MCP layer. `mcp_server.js` communicates with the indexserver via HTTP on localhost (port `PORT+1`). Typesense is internal-only.

### File map

**Client-side (repo root)**

| File | Purpose |
|------|---------|
| `config.py` | Shared constants: HOST, PORT, API_KEY, ROOTS, collection names. Reads `config.json`. |
| `search.py` | Typesense HTTP search; `search()` + `format_results()` |
| `query.py` | tree-sitter AST query functions + `process_file()` + `files_from_search()` |
| `mcp_server.ts` / `mcp_server.js` | Node.js MCP server: `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `manage_service` tools |
| `mcp.cmd` | Windows launcher: `node mcp_server.js` (Linux/WSL: run directly) |
| `ts.cmd` | Thin wrapper: `node ts.mjs %*` |
| `ts.mjs` | Management CLI: start/stop/restart/status/index/verify/log/root/build/setup. Reads `mode` from `config.json`. |
| `setup.cmd` | Thin wrapper: checks Node.js 20+, calls `node setup.mjs %*` |
| `setup.mjs` | Full one-time setup: build MCP, register with Claude Code, WSL env (if --wsl), config.json, start service, VS Code extension |
| `run_tests.cmd` | Thin wrapper: `node run_tests.mjs %*` |
| `run_tests.mjs` | Test runner: `--docker`, `--wsl --destructive`, or `--linux` mode |

**Server-side (`indexserver/`)**

| File | Purpose |
|------|---------|
| `config.py` | Same constants as client config.py; also has INCLUDE_EXTENSIONS, EXCLUDE_DIRS, MAX_FILE_BYTES |
| `api.py` | Single indexserver process: management HTTP API + watcher/heartbeat/verifier threads |
| `indexer.py` | Full re-index via `os.walk` + `.gitignore` parsing + tree-sitter C#/Python metadata extraction. Shared `index_file_list()` pipeline used by both full indexer and verifier. |
| `verifier.py` | Index repair: compares FS mtimes against the index, re-indexes missing/stale files, removes orphaned entries. `check_ready()` for synchronous readiness checks. |
| `watcher.py` | Incremental updates: `PollingObserver` (10 s interval) monitors source root and upserts changes. Uses polling because inotify doesn't fire for Windows-backed `/mnt/` paths in WSL. |
| `start_server.py` | Downloads Typesense Linux binary; starts server process in WSL |
| `service.py` | CLI dispatcher for all `ts` subcommands |
| `smoke_test.py` | Quick sanity check that the server is up and basic queries work |

**Scripts**

| File | Purpose |
|------|---------|
| `scripts/entrypoint.sh` | Docker/WSL container entry point. `--background`: start daemons and exit. `--background --disown`: start + disown (survives session end). Default: Docker foreground mode. |
| `scripts/wsl-setup.sh` | WSL environment setup (venv, Typesense binary) — called by `setup.mjs --wsl` |
| `scripts/e2e.sh` | End-to-end test script (runs inside container/WSL) |
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

The `subsystem` field is the first path component under the source root. Use `sub=` to scope searches to a subsystem.

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
