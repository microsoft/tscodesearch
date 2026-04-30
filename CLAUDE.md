# codesearch — developer notes for Claude

## CRITICAL: no worktrees, no subagents — work serially

**Never use git worktrees or spawn subagents** when working in this repository.
Make all edits directly in the main working directory (`C:\repos\tscodesearch`).
Work step by step so the user can see what is happening at each stage.

## Running tests

WSL tests are **non-destructive** — they start an isolated Typesense on port 18108
(override: `CODESEARCH_TEST_PORT`) using `/tmp/codesearch-wsl-test/` as data dir.
The production instance on port 8108 is never touched.

- **All tests (full suite via Node runner):** `node run_tests.mjs --wsl`
- **Pytest directly (faster, no Typesense needed for unit tests):**
  ```bash
  MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ -v"
  ```
- **Single file:**
  ```bash
  MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/test_cs_throttle.py -v"
  ```
- **Filter by name:**
  ```bash
  MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ -k TestAccessesOn -v"
  ```
- Node runner filter: `node run_tests.mjs --wsl -k TestVerifier`

---

## CRITICAL: running Python scripts from the Bash tool

**Always use the indexserver WSL venv** when running any tscodesearch Python script
(e.g. `tests/inspect_doc.py`, `smoke_test.py`, `indexserver/query_util.py`, utility scripts) via the Bash tool:

```bash
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/python3 tests/inspect_doc.py <args>"
```

To debug AST parsing on a specific file (e.g. why `query_single_file` returns no results):
```bash
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/python3 indexserver/query_util.py --methods /mnt/<drive>/<path>/src/path/to/File.cs"
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/python3 indexserver/query_util.py --declarations MethodName /mnt/<drive>/<path>/src/path/to/File.cs"
```

To hit the indexserver API directly with curl (read API key + port from config.json — never hard-code them):
```bash
# Read key and port from config.json
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String(c.port))")
API_PORT=$((PORT + 1))

# query_single_file — POST /query
curl -s -X POST http://localhost:$API_PORT/query \
  -H "Content-Type: application/json" \
  -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode": "methods", "pattern": "", "files": ["/mnt/c/myproject/src/path/to/File.cs"]}' \
  | python -m json.tool

# query_single_file with a pattern
curl -s -X POST http://localhost:$API_PORT/query \
  -H "Content-Type: application/json" \
  -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode": "declarations", "pattern": "MethodName", "files": ["/mnt/c/myproject/src/path/to/File.cs"]}' \
  | python -m json.tool

# query_codebase — POST /query-codebase
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" \
  -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode": "declarations", "pattern": "MethodName", "root": ""}' \
  | python -m json.tool
```
Run these from the repo root so the `node -e require('./config.json')` resolves correctly.

- `.venv/Scripts/python.exe` is Windows-only and not accessible from Git Bash / Bash tool
- `~/.local/indexserver-venv/` has everything: `typesense`, `tree_sitter_c_sharp`, `tree_sitter`, `watchdog`, `pathspec`, `pytest`
- For tests: `MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ [args]"`

---

## CRITICAL: scripts that run outside Docker must be Node.js

**All scripts invoked from the host (Windows or WSL) must be Node.js — no bash scripts.**

Rationale: Docker mode must have zero WSL dependency. If a script were bash, running it
in Docker mode would require WSL just to invoke the host-side orchestration. Node.js runs
natively on Windows, so it works in every context without a WSL bridge.

Rules:
- Host-side orchestration scripts (test runner, setup, root management, etc.) = `.mjs` / `.js`
- The same Node.js script is used for both Docker mode and WSL mode, with env/platform
  detection inside the script for any environment-specific paths or behaviours
- Bash scripts are only allowed where bash is guaranteed: inside Docker containers or in
  WSL. All such scripts live in `scripts/` (`entrypoint.sh`, `e2e.sh`, `wsl-setup.sh`).
  `docker/` contains only Docker config files (`Dockerfile`, `docker-compose.yml`).

When asked to create or modify a host-side script: write Node.js, not bash.

---

## CRITICAL: fictional names in examples and documentation

When writing or fixing code in this repo — including docstrings, comments, CLI
`--help` text, MCP tool descriptions, and inline examples — **never use real
names from the codebase being searched** (type names, method names, property
names, namespace names, etc.). Always substitute **fictional, generic names**
(e.g. `Widget`, `IRepository`, `SaveChanges`, `ConnectionString`, `Order`).

This applies everywhere: `query.py` docstrings, `mcp_server.ts` tool
descriptions, `CLAUDE.md`, test fixture comments, and any other documentation.
Using real names leaks implementation details into tool metadata that is
visible outside this repository.

## Architecture overview

Two distinct layers that run in separate processes and venvs:

```
┌─────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                               │
│  mcp_server.js  (Node.js — runs on Windows)                 │
│  Claude Code → mcp.cmd  → node mcp_server.js               │
└───────────────────────────┬───────────────────────────────┘
                            │  HTTP  localhost:PORT+1
                            │  (indexserver management API)
┌───────────────────────────▼───────────────────────────────┐
│  INDEXSERVER  (single process: api.py)                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  api.py  — management HTTP server + thread manager  │   │
│  │    • watcher thread    (PollingObserver, /mnt/)      │   │
│  │    • heartbeat thread  (Typesense health check)      │   │
│  │    • syncer thread     (on-demand, via POST /index/start) │
│  └─────────────────────────────────────────────────────┘   │
│  indexer.py   verifier.py   watcher.py   start_server.py   │
│  Venv (WSL only): ~/.local/indexserver-venv/               │
│  Entry: ts.mjs (Node.js management CLI)                    │
└─────────────────────────────────────────────────────────────┘
                         │  data at ~/.local/typesense/
                    Typesense server (Linux binary)
```

The MCP client never runs indexserver code directly — it calls `POST /check-ready`, `POST /index/start`, `POST /verify/start` (alias for `/index/start`), and `POST /verify/stop` on the management API. Syncer status is read from `GET /status` under `syncer.progress`. The management API uses the **same API key** as Typesense (`X-TYPESENSE-API-KEY` header).

## Module map

### Client-side (repo root)

| File | Responsibility |
|------|---------------|
| `src/query/config.py` | Shared constants: `HOST`, `PORT`, `API_KEY`, `ROOTS`, `COLLECTION`, `INCLUDE_EXTENSIONS`. Reads `config.json`. Provides `get_root(name)` → `(collection, src_path)`, `collection_for_root(name)` → `"codesearch_{name}"`, `ROOT_EXTENSIONS`, and `extensions_for_root(name)` for per-root extension filtering. |
| `scripts/search.py` | Test utility: direct Typesense HTTP search. `search(query, ...)` builds params and calls Typesense; `format_results()` prints human-readable output. Run from WSL. |
| `src/ast/cs.py` | C# tree-sitter AST helpers: node type sets, `_find_all`, `_text`, `symbol_kind_query_by`. Used by both `indexer.py` and `src/query/cs.py` to keep extraction consistent. |
| `src/ast/py.py` | Python tree-sitter AST helpers: `_line`, `_py_in_literal`, `_py_enclosing_class`, `_py_base_names`. Also re-exports `_find_all`/`_text` from `ast/cs` (shared traversal helpers). |
| `src/ast/js.py` | JavaScript/TypeScript tree-sitter AST helpers: node type sets, `_find_all`, `_text`, `_line`, `_in_literal`. |
| `src/ast/rust.py` | Rust tree-sitter AST helpers: node type sets, `_find_all`, `_text`, `_line`. |
| `src/ast/cpp.py` | C/C++ tree-sitter AST helpers: node type sets, `_find_all`, `_text`, `_line`. |
| `src/query/cs.py` | C# AST query functions (`q_classes`, `q_methods`, `q_fields`, `q_calls`, `q_implements`, `q_uses`, `q_casts`, `q_all_refs`, `q_attrs`, `q_usings`, `q_declarations`, `q_params`, `q_accesses_on`, `q_accesses_of`, `q_text`) + `process_cs_file()`. Exports `EXTENSIONS = frozenset({".cs"})`. |
| `src/query/py.py` | Python AST query functions (`py_q_classes`, `py_q_methods`, `py_q_calls`, etc.) + `process_py_file()`. Exports `EXTENSIONS = frozenset({".py"})`. |
| `src/query/js.py` | JavaScript/TypeScript AST query functions + `process_js_file()`. Exports `EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})`, `TS_EXTENSIONS = frozenset({".ts", ".tsx"})`, and `TSX_EXTENSIONS = frozenset({".tsx"})`. |
| `src/query/rust.py` | Rust AST query functions + `process_rust_file()`. Exports `EXTENSIONS = frozenset({".rs"})`. |
| `src/query/cpp.py` | C/C++ AST query functions + `process_cpp_file()`. Exports `EXTENSIONS = frozenset({".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"})`. |
| `src/query/dispatch.py` | Pure query layer. Imports all language modules, defines `_make_matches()`, `process_*_file()` functions (raise `ValueError` for unknown modes), `process_any_file()` (routes by extension), and `_ALL_EXTS`. `_EXT_TO_PROCESSOR` is built from each language module's `EXTENSIONS` constant. No CLI, no Typesense, no config dependency — importable standalone. tree-sitter is a hard requirement (no optional fallbacks). |
| `indexserver/query_util.py` | Full CLI entry point. Adds the tscodesearch root to `sys.path`, imports `process_any_file` and `_ALL_EXTS` from `src.query.dispatch`, and provides the complete argparse CLI (all modes, `--json`, `--search`, `--count`, `--context`, etc.). Contains `files_from_search()` (Typesense → local paths) and `expand_files()` (glob expansion). Run this when using the indexserver venv directly, e.g. to debug AST parsing on a specific file. **Must be run via WSL using the indexserver venv** (see below). |
| `mcp_server.ts` / `mcp_server.js` | Node.js MCP server (TypeScript source, compiled to JS). Exposes `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `manage_service` tools. Runs on Windows; communicates with the indexserver via HTTP. |

### `sample/` directory

Checked-in source tree used by `test_sample_e2e.py` for end-to-end indexer tests. Two roots:
- `sample/root1/` — Python and JS/TS fixture files
- `sample/root2/` — additional Python fixture files

### Server-side (`indexserver/`)

| File | Responsibility |
|------|---------------|
| `config.py` | Same constants as client `config.py` — reads the same `codesearch/config.json`. Also has `INCLUDE_EXTENSIONS`, `EXCLUDE_DIRS`, `MAX_FILE_BYTES`, `MAX_CONTENT_CHARS`, `API_PORT = PORT + 1`. Defines `ROOT_EXTENSIONS: dict[str, frozenset | None]` and `extensions_for_root(name)` for per-root extension filtering. Imported by all indexserver modules. |
| `api.py` | **Single indexserver process.** HTTP management API (`ThreadingMixIn + HTTPServer`) on `PORT+1`. Manages three daemon threads: watcher (file watching), heartbeat (Typesense health check, auto-restart), syncer (on-demand scan). Auth: `X-TYPESENSE-API-KEY` header. Endpoints: `GET /health`, `GET /status`, `POST /check-ready`, `POST /index/start`, `POST /verify/start` (alias for `/index/start`), `POST /verify/stop`. Syncer progress is embedded in `GET /status` under `syncer.progress`. Writes `api.pid`; shutdown on SIGTERM. `_run_query` routes files to language processors via `_q._EXT_TO_PROCESSOR` (from `src.query.dispatch`). |
| `indexer.py` | One-shot full index. `run_index(src_root, collection, reset, verbose)` walks the source tree via `os.walk` + `.gitignore` parsing (`pathspec`), calls tree-sitter via `extract_cs_metadata()` / `extract_py_metadata()`, batches upserts via the shared `index_file_list(client, file_pairs, coll_name, batch_size, on_progress, stop_event)` pipeline. `build_schema(name)` returns the collection schema. `walk_source_files(src_root, extensions=None)` is a generator yielding `(full_path, rel)` pairs; respects per-root extension filters. |
| `verifier.py` | Sync/repair logic (used by both startup sync and explicit verify). `run_verify(src_root, collection, queue, delete_orphans, stop_event, extensions, on_complete)` does a two-phase diff: Phase 1 exports the index + walks the FS inline to classify files as missing/stale/orphaned; Phase 2 enqueues changed files to `IndexQueue` and removes orphans. Places an `IndexQueue` fence via `on_complete` so progress reaches `"complete"` after the queue drains. Writes progress to `verifier_progress.json`. `check_ready(src_root, collection, extensions)` runs Phase 1 synchronously and returns `{ready, poll_ok, index_ok, missing, stale, orphaned, fs_files, indexed, duration_s, error}` without modifying the index. |
| `watcher.py` | Incremental updates. `run_watcher(src_root, collection, stop_event)` — started as a daemon thread by `api.py`. `PollingObserver` monitors source root and upserts changed files. Uses `PollingObserver` (not inotify) because source is on a Windows-backed `/mnt/` path. Poll interval is 10 s; detection latency is up to ~12 s (poll + 2 s debounce). Respects per-root extension filters. |
| `heartbeat.py` | Standalone heartbeat watchdog process (alternative to the inline heartbeat thread in `api.py`). Polls `/health` every 30 s; after 3 consecutive failures restarts Typesense via `entrypoint.sh`. Also revives the watcher process if it dies while the server is healthy. Writes `~/.local/typesense/heartbeat.pid`. |
| `index_queue.py` | Centralised, deduplicating index queue. All Typesense writes — from the full-index walk, the WSL watcher, and the Windows native watcher — flow through a single `IndexQueue` instance. Deduplicates by `(collection, file_id)`; a background worker thread batches writes and skips upserts whose mtime hasn't changed since last index. |
| `start_server.py` | Downloads the Typesense Linux binary to `~/.local/typesense/` on first run, starts the process, writes PID to `~/.local/typesense/typesense.pid`. |
| `service.py` | CLI: `start` (Typesense + `api.py`), `stop`, `status` (queries `GET /status` on management API), `restart`, `index [--resethard]`, `verify [--root NAME]` (calls `POST /verify/start`), `log`. All process management is WSL-native using `os.kill`. |
| `smoke_test.py` | Quick sanity check that the server is up and basic queries work. |

## Entry points

| Command | What it does |
|---------|-------------|
| `ts.cmd <cmd>` | Thin wrapper: `node ts.mjs %*` |
| `ts.mjs <cmd>` | Management CLI: start/stop/restart/status/index/verify/log/root/build/setup. Reads `config.json` `mode` field to decide Docker vs WSL. |
| `mcp.cmd` | Thin wrapper: `node mcp_server.js` (Windows). On Linux/WSL: `node mcp_server.js` directly. |
| `setup.cmd [--wsl]` | Thin wrapper: checks Node.js 20+, then calls `node setup.mjs`. |
| `setup.mjs [--wsl]` | Full setup: build MCP server, register with Claude Code, WSL environment (if --wsl), create config.json, start service, install VS Code extension. |
| `run_tests.cmd [args]` | Thin wrapper: `node run_tests.mjs %*`. |
| `run_tests.mjs --docker\|--wsl\|--linux` | Full test runner. |

## Venvs

| Venv | Location | Used by | Packages |
|------|----------|---------|----------|
| Indexserver | `~/.local/indexserver-venv/` | all indexserver modules | `typesense`, `tree_sitter_c_sharp`, `tree_sitter`, `watchdog`, `pathspec`, `pytest` |

> **MCP server is Node.js (`mcp_server.js`), not Python.** `mcp.cmd` (Windows) runs `node mcp_server.js`; on Linux/WSL run it directly. No Python venv is required for the MCP layer.

## config.json

Shared by both layers. Located at `config.json` in the repo root.

```json
{
  "api_key": "codesearch-local",
  "mode": "docker",
  "roots": {
    "default": "C:/myproject/src"
  }
}
```

`mode` is `"docker"` (default) or `"wsl"`. Written by `setup.mjs` with an auto-generated API key. Roots use Windows-style paths (`C:/...`) and are added via `ts root --add` or the VS Code extension.

Old single-root format (`"src_root": "..."`) is auto-promoted to `roots.default` in memory — no file change needed to keep it working.

## Collection naming

`collection_for_root(name)` → `"codesearch_{sanitized_name}"` where sanitized = lowercase alphanumeric + underscores.

Default root → `codesearch_default`. Both `config.py` files compute this identically.

> **After upgrading from the old single-collection setup** (`codesearch_files`), run `ts index --reset` once to create the new `codesearch_default` collection. The `codesearch_files` name is no longer used.

## Tool selection guide

| Goal | Tool |
|------|------|
| Get exact line-level results across the codebase | `query_codebase` |
| Inspect or enumerate contents of one specific file | `query_single_file` |

**`query_codebase`** is the default "just find it" tool:
- Typesense pre-filter narrows to ≤50 files → tree-sitter gives exact lines
- If >50 files match, returns error with subsystem breakdown — never partial results
- Accepts pattern-based modes only (`text`, `declarations`, `calls`, `implements`,
  `uses`, `casts`, `attrs`, `all_refs`, `accesses_on`, `accesses_of`); rejects listing
  modes with a redirect to `query_single_file`
- `uses` accepts optional `uses_kind` to narrow: `field`, `param`, `return`, `cast`, `base`
- Maps AST mode to the best Typesense `query_by` automatically:
  - `uses`/`all_refs`/`accesses_on`/`accesses_of` → `type_refs` field
  - `calls` → `call_sites` field
  - `implements` → `base_types` field
  - `casts` → `cast_sites` field
  - everything else → full-text search

**`query_single_file`** for one file — no Typesense:
- Supports all modes including listing modes (`methods`, `fields`, `classes`,
  `usings`, `imports`) that enumerate file contents without a pattern filter
- Works well on large source files; tree-sitter parses in memory, returns only matching nodes
- Signature: `query_single_file(mode, pattern="", file="", ...)`

## Typesense schema — search mode mapping

| `query_codebase` mode | `query_by` field(s) | What it finds |
|-----------------------|---------------------|---------------|
| `text` (default) | `filename`, `class/method_names`, `content` | Broad keyword search |
| `declarations` | `method_sigs`, `method_names`, `filename` | Methods/types whose signature contains the query [T1] |
| `implements` | `base_types`, `class_names`, `filename` | Files where a type inherits/implements the query [T1] |
| `calls` | `call_sites`, `filename` | Files that call the query method [T1] |
| `uses` | `type_refs`, `symbols`, `class_names`, `filename` | Files that reference the query type in declarations [T2] |
| `attrs` | `attributes`, `filename` | Files decorated with the query attribute [T2] |
| `all_refs` | `type_refs`, `call_sites`, `filename` | All references — broad, catches everything |
| `accesses_on` | `type_refs`, `filename` | Member accesses on instances of a type |
| `accesses_of` | `call_sites`, `filename` | Access sites of a specific property/field name |

T1 fields (`base_types`, `call_sites`, `method_sigs`) are precise tree-sitter extractions.
T2 fields (`type_refs`, `attributes`, `usings`) are broader and may have minor false positives.

## tree-sitter query modes (query.py / query_codebase MCP tool)

`process_cs_file(path, mode, mode_arg, uses_kind, show_path, count_only, context, src_root)` dispatches to:

### C# modes (`.cs` files)

| Mode | mode_arg / uses_kind | Finds |
|------|----------------------|-------|
| `classes` | — | All type declarations with base types *(listing — `query_single_file` only)* |
| `methods` | — | All method/ctor/property/field signatures *(listing — `query_single_file` only)* |
| `fields` | — | All field and property declarations *(listing — `query_single_file` only)* |
| `usings` | — | All using directives *(listing — `query_single_file` only)* |
| `params` | METHOD | Parameter list of METHOD *(listing — `query_single_file` only)* |
| `text` | NAME | Full source of method/type named NAME |
| `declarations` | NAME | Declaration of method/type named NAME |
| `calls` | METHOD | Every call site of METHOD. Accepts bare name (`"Save"`) or qualified name (`"Repo.Save"`) to restrict by receiver class. |
| `implements` | TYPE | Types that inherit/implement TYPE |
| `uses` | TYPE | Every line where TYPE appears as a type reference. Narrow with `uses_kind`: `field` (declared type), `param` (parameter type), `return` (return type), `cast` (cast target), `base` (base type). |
| `casts` | TYPE | Every explicit `(TYPE)expr` cast |
| `all_refs` | NAME | Every identifier occurrence — semantic grep, broader than `uses` |
| `accesses_on` | TYPE | All `.Member` accesses on locals/params typed as TYPE. Handles `var`-inferred locals via `new T()`, array indexing, `as T`, and `(T)` casts. |
| `accesses_of` | MEMBER | Every access site of a property or field named MEMBER. Accepts bare name (`"Status"`) or qualified name (`"Order.Status"`) to restrict by receiver class. |
| `attrs` | NAME? | `[Attribute]` decorators, optionally filtered by name |

### Python modes (`.py` files)

| Mode | mode_arg | Finds |
|------|----------|-------|
| `classes` | — | All class definitions *(listing)* |
| `methods` | — | All function/method definitions *(listing)* |
| `params` | FUNC | Parameters of function FUNC *(listing)* |
| `imports` | — | All import statements *(listing)* |
| `decorators` | NAME? | `@decorator` usages, optionally filtered |
| `declarations` | NAME | Declaration of function/class named NAME |
| `calls` | FUNC | Every call site of FUNC |
| `implements` | CLASS | Classes that inherit from CLASS |
| `ident` | NAME | Every identifier occurrence |

## Testing

### Structural query tests — `tests/test_query_cs.py`

74 tests covering all 15 `query_cs` modes. **No Typesense required** — calls query functions directly against `tests/query_fixture.cs` (a synthetic C# file with no project-specific references).

```bash
node run_tests.mjs --wsl tests/test_query_cs.py
# or directly in WSL:
~/.local/indexserver-venv/bin/pytest tests/test_query_cs.py -v
```

The venv at `/tmp/ts-test-venv` only needs `tree-sitter`, `tree-sitter-c-sharp`, and `pytest` — it is independent of the MCP and indexserver venvs.

### Indexserver / search tests — `tests/`

Tests are split into thematic files. Some require Typesense running (`ts start`); others run standalone.

**CRITICAL: never run `--wsl` and `--docker` tests concurrently.** Both modes bind the same test port (18109). Running them in parallel causes the second to fail with a port conflict and may leave a stale `python3` process holding the port. Always run them sequentially.

**From the Claude Code Bash tool** (the correct way — Windows Node.js, no `wsl.exe node`):
```bash
# WSL mode (Windows host, pytest runs in WSL)
node run_tests.mjs --wsl

# Filter by test name or class
node run_tests.mjs --wsl -k TestQCasts

# Single file
node run_tests.mjs --wsl tests/test_mode_casts.py

# Docker mode
node run_tests.mjs --docker
```

From Windows CMD/PowerShell:
```
node run_tests.mjs --wsl
node run_tests.mjs --wsl -k TestVerifier
node run_tests.mjs --docker
```

From Linux / CI:
```
node run_tests.mjs --linux
node run_tests.mjs --linux -k TestVerifier
```

| File | Class | Server needed | Tests |
|------|-------|--------------|-------|
| `test_query_cs.py` | `TestQueryCs` | **no** | All 15 `query_cs` modes against synthetic `query_fixture.cs` |
| `test_indexer.py` | `TestIndexer` | yes | Collection creation, file count, paths, priority, reset |
| `test_indexer.py` | `TestSemanticFields` | yes | All indexed fields: base_types, call_sites, method_sigs, type_refs, attrs, usings, namespace |
| `test_indexer.py` | `TestMultiRoot` | yes | Two independent collections from the same source tree |
| `test_indexer.py` | `TestExtractCsMetadata` | **no** | Unit tests for C# tree-sitter extractor |
| `test_indexer.py` | `TestSearchFieldModes` | yes | Each MCP search mode's `query_by` field returns the right file |
| `test_indexer.py` | `TestIndexFileList` | **no** | Unit tests for the shared `index_file_list()` batch pipeline |
| `test_indexer_query_consistency.py` | all classes | **no** | Consistency tests verifying indexer and query extract the same values from the same source |
| `test_watcher.py` | `TestCsChangeHandlerUnit` | **no** | Unit tests for watcher event handler logic |
| `test_watcher.py` | `TestCsChangeHandlerIntegration` | yes | Watcher integration: create/modify/delete files, verify index reflects changes |
| `test_process_cs.py` | `TestQueryCs` | **no** | `process_cs_file()` C# structural query modes + consistency with indexer |
| `test_python.py` | `TestExtractPyMetadata` | **no** | Unit tests for Python tree-sitter extractor |
| `test_python.py` | `TestQueryPy` | **no** | `process_py_file()` Python query modes |
| `test_python.py` | `TestPySemanticFields` | yes | Python semantic fields indexed correctly in Typesense |
| `test_verifier.py` | `TestExportIndex` | **no** | Unit tests for `_export_index()` (mock HTTP) |
| `test_verifier.py` | `TestRunVerifyUnit` | **no** | Unit tests for `run_verify()` diff logic (mocked pipeline) |
| `test_verifier.py` | `TestVerifier` | yes | Integration tests: missing files added, stale files reindexed, orphan deletion |
| `test_cpp.py` | all classes | **no** | C/C++ extractor and query functions against `query_fixture.cpp` |
| `test_js_ts.py` | all classes | **no** | JS/TS extractor and query functions against `query_fixture.js` / `.ts` |
| `test_rust.py` | all classes | **no** | Rust extractor and query functions against `query_fixture.rs` |
| `test_mode_callers.py` | unit classes | **no** | `call_sites` field and `q_calls` AST function |
| `test_mode_callers.py` | `TestCallersModeLive` | yes | `calls` mode end-to-end via Typesense |
| `test_mode_symbols.py` | unit classes | **no** | `class_names`/`method_names` fields and text-mode content |
| `test_mode_symbols.py` | `TestSymbolsAndTextModeLive` | yes | `text`/`declarations` mode end-to-end |
| `test_mode_sig.py` | unit classes | **no** | `member_sigs` field, `q_methods`/`q_classes`/`q_fields` semantics, prefilter field selection |
| `test_mode_sig.py` | `TestSigSearchLive` | yes | Signature search end-to-end |
| `test_mode_casts.py` | unit classes | **no** | `q_casts` AST function and cast_types metadata |
| `test_mode_casts.py` | `TestCastTypesLive` | yes | `casts` mode end-to-end |
| `test_mode_attr.py` | unit classes | **no** | `attributes` field and `q_attrs` AST function |
| `test_mode_attr.py` | `TestAttrModeLive` | yes | `attrs` mode end-to-end |
| `test_mode_implements.py` | unit classes | **no** | `base_types` field and `q_implements` AST function |
| `test_mode_implements.py` | `TestImplementsModeLive` | yes | `implements` mode end-to-end |
| `test_mode_uses.py` | unit classes | **no** | `type_refs` field and `q_uses` AST function |
| `test_mode_uses.py` | `TestUsesModeLive` | yes | `uses` mode end-to-end |
| `test_mode_uses_field.py` | all classes | **no** | `uses` with `uses_kind=field` and metadata consistency |
| `test_mode_uses_param.py` | all classes | **no** | `uses` with `uses_kind=param` and metadata consistency |
| `test_mode_accesses_of.py` | `TestQAccessesOf` | **no** | `q_accesses_of` AST function |
| `test_mode_accesses_on.py` | `TestQMemberAccesses` | **no** | `q_accesses_on` AST function |
| `test_mode_all_refs.py` | `TestQIdent` | **no** | `q_all_refs` / `ident` AST function |
| `test_mode_declarations.py` | `TestQFind` | **no** | `q_declarations` AST function |
| `test_mode_params.py` | `TestQParams` | **no** | `q_params` AST function |
| `test_mode_usings.py` | all classes | **no** | `q_usings` AST function and usings field |
| `test_api_dispatch_tables.py` | `TestDispatchConsistency` | **no** | `_EXT_TO_TS_AND_AST` routing table and `_run_query` dispatch table are consistent |
| `test_path_translation.py` | all classes | **no** | `to_native_path`, root parsing, path strip/resolution, Windows↔WSL path logic |
| `test_http_ok.py` | all classes | **no** | `scripts/http_ok.py` health-check helper and entrypoint path |
| `test_sample_e2e.py` | all classes | yes | End-to-end tests against `sample/root1` and `sample/root2`; multi-root, new languages, pre-configured roots, Python AST queries |
| `test_e2e_modes.py` | — | yes | Standalone smoke test script (not pytest); takes a source directory as an argument |

## Common gotchas

**MCP server runs in WSL — file paths must be `/mnt/x/...` inside the process.** `config.json` stores roots as Windows paths (`X:/...`). At runtime, `config.to_native_path()` converts them to the platform-native format: `/mnt/x/...` on Linux, `X:/...` on Windows. If you add any new code that constructs file paths from `SRC_ROOT`, wrap it with `to_native_path()`. This is the root cause of why `files=` glob and `files_from_search()` failed silently before the fix — they produced `c:/myproject/src/...` paths which don't exist in WSL.

**Two `config.py` files that look identical but serve different roles:**
- `src/query/config.py` — imported by Python CLI tools (`scripts/search.py`, `src/query/dispatch.py`)
- `indexserver/config.py` — imported by all indexserver modules

Both read the same `config.json`. If you update config logic, update both.

**`walk_source_files` uses `os.walk` + `.gitignore` parsing (via `pathspec`).** No git is required. Each `.gitignore` found during the walk is loaded and applied relative to its own directory. `EXCLUDE_DIRS` from config prunes directories before gitignore patterns are checked.

**`PollingObserver` in watcher.** The watcher polls every 10 s instead of using inotify because the source tree is on a Windows-backed `/mnt/<drive>/` path (NTFS). Don't switch to `Observer` — inotify doesn't fire for changes made on the Windows side.

**Windows file system watcher lives in the VS Code extension (`vscode-codesearch/src/watcher.ts`), not in the indexserver.** When VS Code is open, `FileWatcher` calls `POST /watcher/pause` to stop the indexserver's `PollingObserver`, then uses VS Code's native `createFileSystemWatcher` (backed by `ReadDirectoryChangesW`) to detect changes and forward batched events to `POST /file-events`. On extension deactivation it calls `POST /watcher/resume` to hand back to the `PollingObserver`. Only Windows-style drive paths (`C:/…`) are watched this way; native Linux/WSL paths stay with the `PollingObserver`.

**stdout capture in mcp_server.js.** `format_results()` and `process_cs_file()` in Python print to stdout; `mcp_server.js` captures that output by spawning the Python helpers as subprocesses. Don't refactor the Python side to return strings — the CLI entry points in `src/query/dispatch.py` depend on the print-based interface.

**PID files live in WSL.** `~/.local/typesense/typesense.pid` (Typesense server) and `~/.local/typesense/api.pid` (indexserver — watcher + heartbeat + verifier threads). `~/.local/typesense/indexer.pid` is shared by `ts index` (subprocess) and the verifier thread (written by `api.py`). They are not in the Windows repo directory. `service.py` uses `os.kill(pid, 0)` (WSL-native) to check liveness.

**`entrypoint.sh --background` vs `--background --disown`.** `--background` alone starts daemons and exits but the processes remain session-attached — when the WSL session ends, they die. This is intentional for tests: clean teardown with no leftover processes. `--background --disown` additionally calls `disown` so the processes survive the WSL session ending. Used by `ts start` and `setup.mjs` for production use.

**`scripts/` vs `docker/`.** `docker/` contains only `Dockerfile` and `docker-compose.yml`. All shell scripts (`entrypoint.sh`, `e2e.sh`, `wsl-setup.sh`) live in `scripts/`. Python files in `indexserver/` reference `scripts/entrypoint.sh`.

**Running tests from the Claude Code Bash tool.** Run `node run_tests.mjs` directly (Windows Node.js), not `wsl.exe node`. The Bash tool runs in Git Bash on Windows, so `node` resolves to the Windows Node.js binary, which is the correct one to use. `wsl.exe node` would look for Node.js inside WSL, which may not be installed.

**Line endings: `.sh` files must stay LF.** `.gitattributes` enforces `eol=lf` for `*.sh`, `Dockerfile`, and `*.py`/`*.json`/`*.toml`, and `eol=crlf` for `*.cmd`/`*.bat`. If you ever see a shell script fail with `\r: command not found`, the file has CRLF endings — fix with `git add --renormalize .` (use `-c core.safecrlf=false` if git refuses due to existing mixed-ending files).
