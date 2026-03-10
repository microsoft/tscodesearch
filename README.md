# codesearch

Full-text and structural code search for a large monorepo. Runs a [Typesense](https://typesense.org) search server in WSL and exposes results as MCP tools so Claude can query the codebase directly without copy-pasting.

## Prerequisites

- Windows 11 with WSL2
- Python 3.10+ available in WSL (`python3 --version`)

## One-time setup

### 1. Register the MCP server and create venvs

From a Windows command prompt, run:

```
setup_mcp.cmd <path-to-your-source-root>
```

Example:
```
setup_mcp.cmd C:\myrepo\src
```

This will:
- Write `config.json` with your source root path and API key
- Create the Windows MCP venv at `.venv\`
- Create the WSL indexserver venv at `~/.local/indexserver-venv/`
- Register `mcp.cmd` with Claude Code

Reload VS Code after running (`Ctrl+Shift+P` → Developer: Reload Window).

### 2. Start the service and build the index

```
ts start          # starts Typesense (WSL), watcher, and heartbeat
```

On first start, `ts start` automatically detects the missing collection and kicks off the indexer. You can also trigger it manually:

```
ts index --reset  # drop + recreate collection, then re-index
```

Initial indexing of a large repo (~100k files) takes 30–40 minutes.

## Docker setup (alternative)

Run codesearch as a Docker container instead of installing locally. The container includes Typesense, the file watcher, and the MCP server.

### Prerequisites

- Docker installed and running
- Source code directory to index

### 1. Build the image

```bash
docker build -t codesearch-mcp -f docker/Dockerfile .
```

### 2. Run the container

```bash
docker run -d --name codesearch \
    -p 3000:3000 \
    -p 8108:8108 \
    -v /path/to/your/source:/source:ro \
    -v codesearch_data:/typesensedata \
    codesearch-mcp
```

Replace `/path/to/your/source` with the path to your source code directory.

- Port `3000` exposes the MCP SSE endpoint
- `/source` is where your code is mounted (read-only)
- `codesearch_data` volume persists the Typesense index between container restarts

On first start, the container will automatically index all files in `/source`.

### 3. Register with Claude Code

```bash
claude mcp add codesearch-docker --transport sse http://localhost:3000/sse
```

### Using docker-compose

Alternatively, use docker-compose for easier management:

```bash
cd docker

# Set your source directory
export SOURCE_DIR=/path/to/your/source

# Start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CODESEARCH_PORT` | `8108` | Typesense server port (internal) |
| `CODESEARCH_ROOT_NAME` | `default` | Name for the source root in config |
| `CODESEARCH_API_KEY` | (auto-generated) | Typesense API key |
| `MCP_PORT` | `3000` | MCP SSE server port |

## Service management

All service commands go through `ts.cmd` (Windows CMD/PowerShell) or `ts.sh` (Git Bash / WSL):

```
ts status                          show server health, doc count, watcher/heartbeat state
ts start                           start Typesense + watcher + heartbeat (auto-indexes if needed)
ts stop                            stop everything
ts restart                         stop then start
ts index                           re-index in background (incremental, keeps existing collection)
ts index --reset                   drop + recreate collection, then re-index
ts index --root <name>             index a specific named root (multi-root setups)
ts verify                          scan FS + repair index: add missing, re-index stale, remove orphans
ts verify --root <name>            verify a specific named root
ts verify --no-delete-orphans      repair without removing deleted-file entries
ts log                             tail the Typesense server log
ts log --indexer [-n N]            tail the indexer/verifier log (default: last 40 lines)
ts log --heartbeat                 tail the heartbeat log
ts watcher                         start the file watcher standalone
ts heartbeat                       start the heartbeat watchdog standalone
```

## Multi-root configuration

To index multiple source trees, edit `config.json`:

```json
{
  "api_key": "codesearch-local",
  "roots": {
    "default": "X:/path/to/first/src",
    "other":   "Y:/path/to/second/src"
  }
}
```

Each root gets its own Typesense collection (`codesearch_default`, `codesearch_other`). Index each one:

```
ts index --root default --reset
ts index --root other   --reset
ts restart
```

Use the MCP `root=` parameter to search a specific collection:
```
search_code("ItemProcessor", root="other")
query_cs("implements", "IFoo", root="other")
```

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

### Structural query tests (no Typesense needed)

```bash
# From WSL — uses a lightweight venv in /tmp:
bash test-query.sh
```

Or directly:
```bash
/tmp/ts-test-venv/bin/pytest tests/test_query_cs.py -v
```

74 tests covering all 15 `query_cs` modes against a synthetic C# fixture (`tests/query_fixture.cs`).

### Indexserver / search tests (some require Typesense running)

Tests are split into thematic files under `tests/`. Tests that don't need a running server (unit tests, mock-based) run anywhere; integration tests auto-skip with a clear message if Typesense is not running.

```
run-server-tests.cmd                       # all tests
run-server-tests.cmd TestSearchFieldModes  # specific class
run-server-tests.cmd test_method_sigs      # specific method
```

Or directly from WSL:
```bash
~/.local/indexserver-venv/bin/pytest tests/ -v
```

| File | What it tests |
|------|---------------|
| `test_indexer.py` | Indexer, semantic fields, multi-root, `extract_cs_metadata`, `index_file_list` pipeline |
| `test_watcher.py` | File watcher event handler (unit + integration) |
| `test_process_cs.py` | `process_file()` C# structural query API |
| `test_python.py` | Python metadata extraction (`extract_py_metadata`), `process_py_file()`, Python semantic fields |
| `test_verifier.py` | `_export_index()` (mock HTTP), `run_verify()` diff logic, full verify integration |

## Direct CLI usage

### Full-text search (`search.py`)

```bash
# Using the Windows venv:
.venv\Scripts\python.exe search.py "MyInterface"
.venv\Scripts\python.exe search.py "MyMethod" --ext cs --sub mysubsystem
.venv\Scripts\python.exe search.py "MyInterface" --mode implements
.venv\Scripts\python.exe search.py "MyMethod"   --mode callers
.venv\Scripts\python.exe search.py "Obsolete"   --mode attr
.venv\Scripts\python.exe search.py "MyType"     --mode uses
```

### Structural C# AST queries (`query.py`)

```bash
.venv\Scripts\python.exe query.py --methods   MyClass.cs
.venv\Scripts\python.exe query.py --calls     MyMethod         "src/mysubsystem/**/*.cs"
.venv\Scripts\python.exe query.py --calls     MyClass.MyMethod "src/mysubsystem/**/*.cs"
.venv\Scripts\python.exe query.py --implements IMyInterface    --search "IMyInterface"
.venv\Scripts\python.exe query.py --field-type MyType          --search "MyType"
.venv\Scripts\python.exe query.py --param-type MyType          --search "MyType"
.venv\Scripts\python.exe query.py --uses      MyType           --search "MyType"
.venv\Scripts\python.exe query.py --find      MyMethod         MyClass.cs
.venv\Scripts\python.exe query.py --attrs     TestMethod       "src/**/*.cs"
.venv\Scripts\python.exe query.py --member-accesses MyType     MyClass.cs
```

## Architecture

### Two-layer search

1. **Typesense** — fast keyword/semantic search over pre-indexed metadata (class names, method names, base types, call sites, signatures, attributes, etc.). Runs in WSL; data stored at `~/.local/typesense/`.

2. **tree-sitter** — precise C# AST queries on the file set returned by Typesense. Skips comments and string literals, understands syntax.

Typical flow: Typesense narrows the haystack to ~50 candidate files → tree-sitter parses each one and applies the structural query.

### Process topology

```
┌─────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                   │
│  mcp_server.py   search.py   query.py           │
│  Claude Code VSCode ext → mcp.sh  (WSL)  ← actual
│  Manual/CI alternative  → mcp.cmd (Windows)     │
│  Venv (WSL):     ~/.local/mcp-venv/             │
│  Venv (Windows): .venv/                         │
└───────────────────┬─────────────────────────────┘
                    │ HTTP localhost:8108
┌───────────────────▼─────────────────────────────┐
│  INDEXSERVER  (WSL only)                        │
│  indexserver/service.py    indexer.py           │
│  indexserver/watcher.py    heartbeat.py         │
│  Venv: ~/.local/indexserver-venv/               │
│  Entry: ts.cmd (Windows) / ts.sh (WSL)          │
└─────────────────────────────────────────────────┘
                    │ data
             Typesense server
          ~/.local/typesense/
```

> **MCP runs in WSL.** The Claude Code VSCode extension launches the MCP server via `mcp.sh`, so `mcp_server.py` runs under the WSL Python (`~/.local/mcp-venv`). This means file paths inside the MCP process must be `/mnt/x/...` style, even though `config.json` stores them as Windows `X:/...` paths. `config.to_native_path()` converts automatically based on `sys.platform`.
>
> Direct CLI usage (`query.py`, `search.py` invoked by hand) can run under either Windows or WSL depending on which Python you call — both are supported.

### Docker topology

When running in Docker, all components run in a single container:

```
┌─────────────────────────────────────────────────┐
│  DOCKER CONTAINER                               │
│                                                 │
│  MCP Server (SSE) ──────────── port 3000        │
│  Typesense Server ──────────── port 8108        │
│  File Watcher (background)                      │
│                                                 │
│  /source (volume) ──── your source code         │
│  /typesensedata (volume) ── persisted index     │
└─────────────────────────────────────────────────┘
```

The MCP server uses SSE (Server-Sent Events) transport instead of stdio, allowing Claude Code to connect via HTTP.

### File map

**Client-side (repo root)**

| File | Purpose |
|------|---------|
| `config.py` | Shared constants: HOST, PORT, API_KEY, ROOTS, collection names. Reads `config.json`. |
| `search.py` | Typesense HTTP search; `search()` + `format_results()` |
| `query.py` | tree-sitter AST query functions + `process_file()` + `files_from_search()` |
| `mcp_server.py` | FastMCP server: `search_code`, `query_cs`, `query_py`, `ready`, `verify_index`, `service_status` tools |
| `mcp.cmd` | Windows launcher: `.venv\Scripts\python.exe mcp_server.py` |
| `mcp.sh` | WSL launcher: `~/.local/mcp-venv/bin/python mcp_server.py` |
| `setup_mcp.cmd` | One-time setup: writes config.json, creates venvs, registers MCP |

**Server-side (`indexserver/`)**

| File | Purpose |
|------|---------|
| `config.py` | Same constants as client config.py; also has INCLUDE_EXTENSIONS, EXCLUDE_DIRS, MAX_FILE_BYTES |
| `indexer.py` | Full re-index via `os.walk` + `.gitignore` parsing + tree-sitter C#/Python metadata extraction. Shared `index_file_list()` pipeline used by both full indexer and verifier. |
| `verifier.py` | Index repair: compares FS mtimes against the index, re-indexes missing/stale files, removes orphaned entries. `check_ready()` for synchronous readiness checks. |
| `watcher.py` | Incremental updates: `PollingObserver` (10 s interval) monitors source root and upserts changes. Uses polling because inotify doesn't fire for Windows-backed `/mnt/` paths in WSL. |
| `heartbeat.py` | Health loop: checks server every 30 s, restarts watcher or server on failure |
| `start_server.py` | Downloads Typesense Linux binary; starts server process in WSL |
| `service.py` | CLI dispatcher for all `ts` subcommands including `ts verify` |
| `smoke_test.py` | Quick sanity check that the server is up and basic queries work |

**Entry points**

| File | Purpose |
|------|---------|
| `ts.cmd` | Windows CMD/PowerShell → WSL bridge for all service commands |
| `ts.sh` | WSL / Git Bash entry point for service commands |
| `smoke-test.cmd` | Run smoke_test.py via WSL |
| `run-server-tests.cmd` | Run pytest test suite via WSL |

### Typesense schema

The collection uses tiered semantic fields extracted by tree-sitter at index time:

| Tier | Fields | Used by MCP mode |
|------|--------|-----------------|
| T1 | `base_types` | `implements` |
| T1 | `call_sites` | `callers` |
| T1 | `method_sigs` | `sig` |
| T2 | `type_refs` | `uses` |
| T2 | `attributes` | `attr` |
| T2 | `usings` | — |
| — | `class_names`, `method_names`, `symbols` | `text`, `symbols` |
| — | `content` | `text` |

Search ranking by file type: `.cs` (priority 3) → `.h/.cpp/.c` (2) → scripts/`.py/.ts` (1) → config/docs (0).

The `subsystem` field is the first path component under the source root. Use `sub=` to scope searches to a subsystem.

### config.json

```json
{
  "api_key": "codesearch-local",
  "roots": {
    "default": "X:/path/to/your/src"
  }
}
```

This file is **not checked in** (listed in `.gitignore`) — it contains your local source root path. Run `setup_mcp.cmd <src-root>` to generate it.
