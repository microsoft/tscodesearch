# codesearch — developer notes for Claude

## CRITICAL: no worktrees, no subagents — work serially

**Never use git worktrees or spawn subagents.** Make all edits directly in the main working directory (`Q:\spocore\tscodesearch`). Work step by step.

## CRITICAL: running Python scripts from the Bash tool

Everything runs in the **client venv** on Windows — there is no separate WSL venv anymore.

```bash
.client-venv/Scripts/python.exe <script> <args>
.client-venv/Scripts/python.exe -m pytest tests/ query/tests/ -v
```

AST debug:
```bash
.client-venv/Scripts/python.exe -m query --mode methods --file C:/myproject/src/Widget.cs
```

Management API via curl (read key/port from config.json — never hard-code):
```bash
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String(c.port??8108))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

The `X-TYPESENSE-API-KEY` header name is kept for backwards compatibility with existing callers; the daemon checks any value matches `config.json`'s `api_key`.

## CRITICAL: host-side orchestration scripts must be Node.js

All orchestration scripts invoked from the host = `.mjs`/`.js`. The MCP server is Python (`mcp_server.py`) via `.client-venv\Scripts\python.exe`.

## CRITICAL: fictional names in examples and documentation

Never use real names from the searched codebase (types, methods, namespaces) in docstrings, comments, CLI help, tool descriptions, or examples. Always use fictional generics: `Widget`, `IRepository`, `SaveChanges`, `Order`.

---

## Architecture

```
Windows side
────────────────────────────────────────────────────────────────
  tsquery_server.py (daemon)          mcp_server.py (MCP stdio)
  started by ts start                  started by Claude Code
  owns the management API port         calls HTTP API at port
        │◄──────────────── port ────────────────────────────────┤
        ├── ThreadingHTTPServer on PORT    ← VS Code extension
        ├── watchdog Observer (ReadDirectoryChangesW on Windows)
        ├── IndexQueue worker (batch Tantivy writes)
        ├── Syncer (verify/index jobs)
        └── Tantivy backends (one per root, on-disk in <repo>/.tantivy/)
```

`tsquery_server.start_daemon()` tries to bind PORT; returns `False` if another instance is already running. There is no separate Typesense process: the search index lives in-process via `tantivy-py`.

Management API endpoints: `GET /health`, `GET /status`, `POST /check-ready`, `POST /index/start`, `POST /verify/start`, `POST /verify/stop`, `POST /query-codebase`, `POST /file-events`, `POST /management/shutdown`.

`query_single_file` bypasses HTTP entirely — calls `query_file()` from `query/dispatch.py` in-process. Works without the daemon.

---

## Module map

### Backend (search index)

| File | Responsibility |
|------|---------------|
| `indexserver/backend.py` | Tantivy schema definition + `Backend` (write/read/upsert/delete/export). One Tantivy index per "collection"; on-disk directory `<repo>/.tantivy/<collection>/`. |
| `indexserver/search.py` | `search()` — Typesense-shaped result dict on top of `Backend`. Translates `query_by`/`weights`/`num_typos`/`filter_by` into Tantivy queries. |

### Daemon + MCP

| File | Responsibility |
|------|---------------|
| `tsquery_server.py` | Cross-platform management daemon. HTTP + watcher + queue + syncer threads. Opens one `Backend` per root at startup. |
| `mcp_server.py` | FastMCP server. Exposes `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `wait_for_sync`. Calls `tsquery_server.start_daemon()` at startup. `--daemon` runs as a standalone daemon. |

### Query (AST)

| File | Responsibility |
|------|---------------|
| `query/cs.py`, `query/py.py`, `query/js.py`, `query/rust.py`, `query/cpp.py`, `query/sql.py` | Per-language tree-sitter AST functions and bytes-level mode handlers. |
| `query/dispatch.py` | Pure query layer. `query_file(src_bytes, ext, mode, mode_arg, ...)`, `describe_file()`, `ALL_EXTS`. No backend dependency. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs`. JSON stdin mode also supported. |

### Indexer

| File | Responsibility |
|------|---------------|
| `indexserver/config.py` | Reads `config.json`. Roots and extensions. |
| `indexserver/indexer.py` | `run_index()`, `walk_source_files()`, `index_file_list()`, `ensure_backend()`. |
| `indexserver/verifier.py` | `run_verify()` (two-phase diff + repair), `check_ready()` (read-only health check). |
| `indexserver/watcher.py` | `run_watcher()`. `watchdog.observers.Observer` on Windows (real-time), `PollingObserver` on Linux/WSL. |
| `indexserver/index_queue.py` | Deduplicated batch queue. Writes go through a `BackendResolver` (`collection_name → Backend`). |
| `indexserver/query_util.py` | Structural query CLI (`python -m indexserver.query_util ...`). `--search` opens the backend in read-only mode. |

### Scripts

| File | Responsibility |
|------|---------------|
| `scripts/search.py` | Standalone search CLI. Opens a read-only `Backend` and calls `indexserver.search.search()`. |
| `ts.mjs` | Daemon CLI: `start`/`stop`/`restart`/`status`/`index`/`verify`/`log`/`root`. Just spawns `tsquery_server.py` and posts to its API. |
| `setup.mjs` | Creates `.client-venv`, registers MCP, installs the VS Code extension. |
| `run_tests.mjs` | VS Code extension unit tests (no daemon required). |

---

## Entry points

| Command | What it does |
|---------|-------------|
| `ts.cmd <cmd>` | `node ts.mjs %*` |
| `mcp.cmd` | `.client-venv\Scripts\python.exe mcp_server.py` |
| `setup.cmd` | `node setup.mjs` |
| `run_tests.cmd` | `node run_tests.mjs` — VS Code tests |

## Venvs

| Venv | Location | Packages |
|------|----------|----------|
| Client | `.client-venv/` (Windows) | `mcp`, `tree-sitter`, all grammar packages, `tantivy`, `watchdog`, `pathspec`, `pytest` |

There is **no longer a WSL or indexserver venv** — Tantivy runs in-process in the same `.client-venv` Python.

## config.json

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": { "default": "C:/myproject/src" }
}
```

`port` is the daemon's HTTP API port (single port, no Typesense+1). Roots use Windows paths (`C:/...`). `collection_for_root(name)` → `"codesearch_{sanitized_name}"` (default → `codesearch_default`). Each collection's index lives at `<repo>/.tantivy/<collection>/`.

---

## Tool selection guide

| Goal | Tool |
|------|------|
| Exact line-level results across the codebase | `query_codebase` |
| Inspect/enumerate one specific file | `query_single_file` |

**`query_codebase`**: Tantivy pre-filter → ≤50 files → tree-sitter exact lines. Returns folder breakdown (one level deeper than the current `sub=` scope) if >50 files match. Pattern-based modes only; listing modes redirect to `query_single_file`. `uses` accepts `uses_kind`: `field`, `param`, `return`, `cast`, `base`. `sub=` accepts any folder depth (`services` or `services/billing`).

**`query_single_file`**: No backend search. Supports listing modes (`methods`, `fields`, `classes`, `usings`, `imports`). Works offline.

## Backend schema — search mode mapping

| Mode | `query_by` field(s) | Notes |
|------|---------------------|-------|
| `declarations` | `member_sigs`, `method_names`, `filename` | Precise [T1] |
| `implements` | `base_types`, `class_names`, `filename` | Precise [T1] |
| `calls` | `call_sites`, `filename` | Precise [T1] |
| `uses` | `type_refs`, `class_names`, `filename` | Broader [T2] |
| `attrs` | `attr_names`, `filename` | Broader [T2] |
| `all_refs` | `filename`, `class_names`, `method_names`, `tokens` | Broadest |
| `accesses_on` | `type_refs`, `filename` | Member accesses on type instances |
| `accesses_of` | `member_accesses`, `filename` | Access sites of a property/field name |

T1 = precise tree-sitter extractions. T2 = broader, minor false positives possible.

`tokens` is the per-file deduped bag of identifiers extracted by the same tree-sitter walk that drives `all_refs` — identifiers inside string literals, char literals, and comments are excluded. The structural fields (`class_names`, `method_names`, `member_sigs`, `type_refs`, `call_sites`, `member_accesses`, etc.) are also pre-split per-language by the AST extractors, so each entry stored in the index is a single identifier.

## tree-sitter query modes

### C# (`.cs`)

| Mode | Arg | Finds |
|------|-----|-------|
| `classes`, `methods`, `fields`, `usings` | — | Listing *(query_single_file only)* |
| `params` | METHOD | Parameter list *(query_single_file only)* |
| `declarations` | NAME | Declaration of method/type |
| `calls` | METHOD | Call sites. `"Repo.Save"` restricts by receiver. |
| `implements` | TYPE | Types that inherit/implement TYPE |
| `uses` | TYPE | Type references. Narrow with `uses_kind`. |
| `casts` | TYPE | `(TYPE)expr` casts |
| `all_refs` | NAME | Every identifier occurrence |
| `accesses_on` | TYPE | `.Member` accesses on locals typed as TYPE |
| `accesses_of` | MEMBER | Access sites of property/field. `"Order.Status"` restricts. |
| `attrs` | NAME? | `[Attribute]` decorators |

### Python (`.py`)

| Mode | Arg | Finds |
|------|-----|-------|
| `classes`, `methods`, `params`, `imports` | — | Listing *(query_single_file only)* |
| `decorators` | NAME? | `@decorator` usages |
| `declarations` | NAME | Function/class declaration |
| `calls` | FUNC | Call sites |
| `implements` | CLASS | Subclasses |
| `ident` | NAME | Every identifier occurrence |

---

## Testing

| Directory | Backend needed | Contents |
|-----------|----------------|----------|
| `tests/unit/*.py` | **no** (uses `_FakeBackend`) | Unit tests: extractors, queue, verifier diff, path translation, mcp_server helpers |
| `tests/integration/*.py` | **yes** | Integration tests: indexer, verifier, watcher, search modes live, sample e2e — each opens a fresh Tantivy index in `<repo>/.tantivy/` |
| `query/tests/*.py` | **no** | AST query unit tests for all C# modes and edge cases |

```bash
# Full suite (1200+ tests)
.client-venv/Scripts/python.exe -m pytest tests/ query/tests/ -v

# Filter by test name
.client-venv/Scripts/python.exe -m pytest tests/ -k TestQCasts -v

# Single file
.client-venv/Scripts/python.exe -m pytest tests/unit/test_watcher.py -v

# VS Code extension tests (no daemon required)
node run_tests.mjs
```

The integration `conftest.py` writes a temporary `config.json` pointing at `sample/root1` and `sample/root2` and sets `CODESEARCH_CONFIG`. No external service to start.

---

## Common gotchas

**Windows paths.** `config.json` roots are Windows-style (`C:/...`). The daemon, the MCP server, and the Tantivy index all run on Windows; backslashes in any path input are normalised to forward slashes at the boundary.

**Watcher observer selection.** `watcher.py` uses `watchdog.observers.Observer` on Windows (ReadDirectoryChangesW, ~1 s latency) and `PollingObserver` on Linux/WSL. Don't hardcode either.

**Tantivy is single-writer.** The daemon owns one `IndexWriter` per collection. CLIs (`scripts/search.py`, `indexserver.query_util --search`) open the index read-only via `ensure_backend(..., write=False)`. Trying to open a writer while the daemon already has one will block or fail — let the daemon do the writing and search via the HTTP API.

**Index location.** `<repo>/.tantivy/<collection>/`. Wipe with `ts index --resethard`, or remove the directory directly.

**Line endings.** `.gitattributes` enforces LF for shell scripts. Fix with `git add --renormalize .` if any cross-OS quoting goes wrong.
