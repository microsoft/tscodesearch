# codesearch — developer notes for Claude

## CRITICAL: running Python scripts from the Bash tool

**Always use the indexserver WSL venv** when running any tscodesearch Python script
(e.g. `inspect_doc.py`, `smoke_test.py`, utility scripts) via the Bash tool:

```bash
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "~/.local/indexserver-venv/bin/python3 /mnt/q/spocore/tscodesearch/inspect_doc.py <args>"
```

- `.venv/Scripts/python.exe` is Windows-only and not accessible from Git Bash / Bash tool
- `~/.local/mcp-venv/` is the MCP client venv — it lacks `typesense` and `pathspec`
- `~/.local/indexserver-venv/` has everything: `typesense`, `tree_sitter_c_sharp`, `tree_sitter`, `watchdog`, `pathspec`, `pytest`
- For tests: `MSYS_NO_PATHCONV=1 wsl.exe bash -lc "~/.local/indexserver-venv/bin/pytest /mnt/q/spocore/tscodesearch/tests/ [args]"`

---

## CRITICAL: fictional names in examples and documentation

When writing or fixing code in this repo — including docstrings, comments, CLI
`--help` text, MCP tool descriptions, and inline examples — **never use real
names from the codebase being searched** (type names, method names, property
names, namespace names, etc.). Always substitute **fictional, generic names**
(e.g. `Widget`, `IRepository`, `SaveChanges`, `ConnectionString`, `Order`).

This applies everywhere: `query.py` docstrings, `mcp_server.py` tool
descriptions, `CLAUDE.md`, test fixture comments, and any other documentation.
Using real names leaks implementation details into tool metadata that is
visible outside this repository.

## Architecture overview

Two distinct layers that run in separate processes and venvs:

```
┌─────────────────────────────────────────────────────────────┐
│  MCP CLIENT  (Claude ↔ tools)                               │
│  mcp_server.py   search.py   query.py   config.py           │
│  Claude Code VSCode ext → mcp.sh  (WSL)  ← actual in use   │
│  Manual/CI alternative  → mcp.cmd (Windows)                 │
│  Venv (WSL):     ~/.local/mcp-venv/bin/python               │
│  Venv (Windows): codesearch/.venv/Scripts/python.exe        │
└───────┬────────────────────────┬───────────────────────────┘
        │  HTTP  localhost:8108  │  HTTP  localhost:PORT+1
        │  (Typesense search)    │  (indexserver management API)
┌───────▼────────────────────────▼───────────────────────────┐
│  INDEXSERVER  (single process: api.py)                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  api.py  — management HTTP server + thread manager  │   │
│  │    • watcher thread    (PollingObserver, /mnt/)      │   │
│  │    • heartbeat thread  (Typesense health check)      │   │
│  │    • verifier thread   (on-demand, via POST /verify) │   │
│  └─────────────────────────────────────────────────────┘   │
│  indexer.py   verifier.py   watcher.py   start_server.py   │
│  Venv (WSL only): ~/.local/indexserver-venv/               │
│  Entry: ts.cmd (Windows→WSL bridge) / ts.sh (WSL direct)   │
└─────────────────────────────────────────────────────────────┘
                         │  data at ~/.local/typesense/
                    Typesense server (Linux binary)
```

The MCP client never runs indexserver code directly — it calls `POST /check-ready`, `POST /verify/start`, `GET /verify/status`, and `POST /verify/stop` on the management API. The management API uses the **same API key** as Typesense (`X-TYPESENSE-API-KEY` header).

## Module map

### Client-side (repo root)

| File | Responsibility |
|------|---------------|
| `config.py` | Shared constants: `HOST`, `PORT`, `API_KEY`, `ROOTS`, `COLLECTION`, `INCLUDE_EXTENSIONS`. Reads `config.json`. Provides `get_root(name)` → `(collection, src_path)` and `collection_for_root(name)` → `"codesearch_{name}"`. |
| `search.py` | HTTP search wrapper. `search(query, ...)` builds params and calls Typesense; `format_results()` prints human-readable output. Used by `mcp_server.py`. |
| `query.py` | Tree-sitter AST query functions (`q_classes`, `q_methods`, `q_fields`, `q_calls`, `q_implements`, `q_uses`, `q_casts`, `q_all_refs`, `q_attrs`, `q_usings`, `q_declarations`, `q_params`, `q_accesses_on`, `q_accesses_of`, `q_text`). `process_file(path, mode, mode_arg, uses_kind, ...)` dispatches to them and prints matches. `files_from_search()` resolves Typesense hits to local file paths. |
| `mcp_server.py` | FastMCP server. Exposes `search_code`, `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status` tools. Captures stdout with `StringIO`. Supports multi-root via `root=` parameter. |

### Server-side (`indexserver/`)

| File | Responsibility |
|------|---------------|
| `config.py` | Same constants as client `config.py` — reads the same `codesearch/config.json`. Also has `INCLUDE_EXTENSIONS`, `EXCLUDE_DIRS`, `MAX_FILE_BYTES`, `MAX_CONTENT_CHARS`, `API_PORT = PORT + 1`. Imported by all indexserver modules. |
| `api.py` | **Single indexserver process.** HTTP management API (`ThreadingMixIn + HTTPServer`) on `PORT+1`. Manages three daemon threads: watcher (file watching), heartbeat (Typesense health check, auto-restart), verifier (on-demand scan). Auth: `X-TYPESENSE-API-KEY` header. Endpoints: `GET /health`, `GET /status`, `POST /check-ready`, `POST /verify/start`, `GET /verify/status`, `POST /verify/stop`. Writes `api.pid`; shutdown on SIGTERM. |
| `indexer.py` | One-shot full index. `run_index(src_root, collection, reset, verbose)` walks the source tree via `os.walk` + `.gitignore` parsing (`pathspec`), calls tree-sitter via `extract_cs_metadata()` / `extract_py_metadata()`, batches upserts via the shared `index_file_list(client, file_pairs, coll_name, batch_size, on_progress, stop_event)` pipeline. `build_schema(name)` returns the collection schema. `walk_source_files(src_root)` is a generator yielding `(full_path, rel)` pairs. |
| `verifier.py` | Index repair. `run_verify(src_root, collection, delete_orphans, stop_event)` does a two-phase diff: Phase 1 exports the index + walks the FS inline to classify files as missing/stale/orphaned; Phase 2 calls `index_file_list()` for only changed files and removes orphans. Writes progress to `verifier_progress.json`. `check_ready(src_root, collection)` runs Phase 1 synchronously and returns `{ready, poll_ok, index_ok, missing, stale, orphaned, fs_files, indexed, duration_s, error}` without modifying the index. |
| `watcher.py` | Incremental updates. `run_watcher(src_root, collection, stop_event)` — started as a daemon thread by `api.py`. `PollingObserver` monitors source root and upserts changed files. Uses `PollingObserver` (not inotify) because source is on a Windows-backed `/mnt/` path. Poll interval is 10 s; detection latency is up to ~12 s (poll + 2 s debounce). |
| `start_server.py` | Downloads the Typesense Linux binary to `~/.local/typesense/` on first run, starts the process, writes PID to `~/.local/typesense/typesense.pid`. |
| `service.py` | CLI: `start` (Typesense + `api.py`), `stop`, `status` (queries `GET /status` on management API), `restart`, `index [--resethard]`, `verify [--root NAME]` (calls `POST /verify/start`), `log`. All process management is WSL-native using `os.kill`. |
| `smoke_test.py` | Quick sanity check that the server is up and basic queries work. |

## Entry points

| Command | What it does |
|---------|-------------|
| `ts.cmd <cmd>` | Windows CMD/PowerShell → WSL bridge. Strips trailing `\` from `%~dp0`, converts with `wslpath -u`, then runs `ts.sh` in WSL. |
| `ts.sh <cmd>` | WSL / Git Bash entry point. From Git Bash: `MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/path/to/tscodesearch/ts.sh <cmd>`. |
| `mcp.cmd` | Runs `mcp_server.py` under `.venv/Scripts/python.exe` (Windows). |
| `mcp.sh` | Runs `mcp_server.py` under `~/.local/mcp-venv/bin/python` (WSL). |
| `setup_mcp.cmd <src-dir>` | One-time setup: writes `config.json`, creates venvs, registers MCP with Claude Code. |
| `smoke-test.cmd` | Runs `indexserver/smoke_test.py` via WSL indexserver venv. |
| `run-server-tests.cmd [filter]` | Runs all tests in `tests/` via WSL indexserver venv + pytest. |

## Venvs

| Venv | Location | Used by | Packages |
|------|----------|---------|----------|
| MCP (WSL) | `~/.local/mcp-venv/` | `mcp.sh` → `mcp_server.py` — **used by Claude Code VSCode ext** | `mcp`, `tree_sitter_c_sharp`, `tree_sitter` |
| MCP (Windows) | `.venv/` | `mcp.cmd` → `mcp_server.py` — alternative, not used by extension | same as above |
| Indexserver | `~/.local/indexserver-venv/` | `ts.cmd/ts.sh` → all indexserver modules | `typesense`, `tree_sitter_c_sharp`, `tree_sitter`, `watchdog`, `pathspec`, `pytest` |

> **The indexserver and MCP client have separate tree-sitter parsers.** Both parse C# correctly — they just run in different processes. Do not confuse `codesearch.query` (MCP-side) with `codesearch.indexserver.indexer` (indexer-side) when tracing a bug.

> **MCP runs in WSL; CLI can be Windows or WSL.** The Claude Code VSCode extension always launches `mcp.sh`, so `mcp_server.py` runs as a Linux process (`sys.platform == "linux"`). Direct CLI invocations of `query.py` or `search.py` can run under either the Windows venv or the WSL venv. Both are supported — `config.to_native_path()` converts `X:/...` ↔ `/mnt/x/...` based on `sys.platform`.

## config.json

Shared by both layers. Located at `config.json` in the repo root.

```json
{
  "api_key": "codesearch-local",
  "roots": {
    "default": "C:/myproject/src"
  }
}
```

Old single-root format (`"src_root": "..."`) is auto-promoted to `roots.default` in memory — no file change needed to keep it working.

## Collection naming

`collection_for_root(name)` → `"codesearch_{sanitized_name}"` where sanitized = lowercase alphanumeric + underscores.

Default root → `codesearch_default`. Both `config.py` files compute this identically.

> **After upgrading from the old single-collection setup** (`codesearch_files`), run `ts index --reset` once to create the new `codesearch_default` collection. The `codesearch_files` name is no longer used.

## Tool selection guide

| Goal | Tool |
|------|------|
| Find which files mention a symbol (file-level, fast) | `search_code` |
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

| `search_code` mode | `query_by` field(s) | What it finds |
|--------------------|---------------------|---------------|
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

`process_file(path, mode, mode_arg, uses_kind, show_path, count_only, context, src_root)` dispatches to:

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
# Quick run via helper script (creates /tmp/ts-test-venv if needed):
bash test-query.sh

# Or directly:
/tmp/ts-test-venv/bin/pytest tests/test_query_cs.py -v
```

The venv at `/tmp/ts-test-venv` only needs `tree-sitter`, `tree-sitter-c-sharp`, and `pytest` — it is independent of the MCP and indexserver venvs.

### Indexserver / search tests — `tests/`

Tests are split into thematic files. Some require Typesense running (`ts start`); others run standalone.

**From the Claude Code Bash tool** (the correct way — avoids path-conversion problems):
```bash
# All tests
MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/<drive>/path/to/tscodesearch/run_tests.sh

# Filter by test name or class
MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/<drive>/path/to/tscodesearch/run_tests.sh -k TestQCasts

# Single file
MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/<drive>/path/to/tscodesearch/run_tests.sh tests/test_mode_casts.py
```

From WSL directly:
```bash
bash /mnt/<drive>/path/to/tscodesearch/run_tests.sh
bash /mnt/<drive>/path/to/tscodesearch/run_tests.sh -k TestVerifier
```

From Windows CMD/PowerShell:
```
run-server-tests.cmd
run-server-tests.cmd TestVerifier
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
| `test_process_cs.py` | `TestQueryCs` | **no** | `process_file()` C# structural query modes + consistency with indexer |
| `test_python.py` | `TestExtractPyMetadata` | **no** | Unit tests for Python tree-sitter extractor |
| `test_python.py` | `TestQueryPy` | **no** | `process_py_file()` Python query modes |
| `test_python.py` | `TestPySemanticFields` | yes | Python semantic fields indexed correctly in Typesense |
| `test_verifier.py` | `TestExportIndex` | **no** | Unit tests for `_export_index()` (mock HTTP) |
| `test_verifier.py` | `TestRunVerifyUnit` | **no** | Unit tests for `run_verify()` diff logic (mocked pipeline) |
| `test_verifier.py` | `TestVerifier` | yes | Integration tests: missing files added, stale files reindexed, orphan deletion |

## Common gotchas

**MCP server runs in WSL — file paths must be `/mnt/x/...` inside the process.** `config.json` stores roots as Windows paths (`X:/...`) because `setup_mcp.cmd` writes them from Windows. At runtime, `config.to_native_path()` converts them to the platform-native format: `/mnt/x/...` on Linux, `X:/...` on Windows. If you add any new code that constructs file paths from `SRC_ROOT`, wrap it with `to_native_path()`. This is the root cause of why `files=` glob and `files_from_search()` failed silently before the fix — they produced `c:/myproject/src/...` paths which don't exist in WSL.

**Two `config.py` files that look identical but serve different roles:**
- `codesearch/config.py` — imported by MCP client (`search.py`, `query.py`, `mcp_server.py`)
- `codesearch/indexserver/config.py` — imported by all indexserver modules

Both read the same `codesearch/config.json`. If you update config logic, update both.

**`walk_source_files` uses `os.walk` + `.gitignore` parsing (via `pathspec`).** No git is required. Each `.gitignore` found during the walk is loaded and applied relative to its own directory. `EXCLUDE_DIRS` from config prunes directories before gitignore patterns are checked.

**`PollingObserver` in watcher.** The watcher polls every 10 s instead of using inotify because the source tree is on a Windows-backed `/mnt/<drive>/` path (NTFS). Don't switch to `Observer` — inotify doesn't fire for changes made on the Windows side.

**stdout capture in mcp_server.py.** `format_results()` and `process_file()` print to stdout. `mcp_server.py` captures with `StringIO`. Don't refactor these to return strings — the CLI entry points in `query.py` depend on the print-based interface.

**PID files live in WSL.** `~/.local/typesense/typesense.pid` (Typesense server) and `~/.local/typesense/api.pid` (indexserver — watcher + heartbeat + verifier threads). `~/.local/typesense/indexer.pid` is shared by `ts index` (subprocess) and the verifier thread (written by `api.py`). They are not in the Windows repo directory. `service.py` uses `os.kill(pid, 0)` (WSL-native) to check liveness.

**cmd→WSL path conversion: never pass a trailing backslash to wslpath.** `%~dp0` always ends with `\`, so `wsl wslpath -u "%~dp0"` produces `"C:\path\"` where the `\"` is parsed by CommandLineToArgvW as an escaped quote, leaving the string unclosed. Always strip the trailing backslash first:
```cmd
set "_WIN=%~dp0"
for /f "usebackq tokens=*" %%W in (`wsl wslpath -u "%_WIN:~0,-1%"`) do set "_WSLDIR=%%W/"
```
The explicit `%%W/` re-adds the trailing slash after wslpath (which strips it).

**Shell script `$REPO` is relative to the script's own location.** `ts.sh` sets `REPO=$(cd "$(dirname "$0")" && pwd)` — do not prepend the repo directory name again when building paths to `indexserver/service.py`.

**Running `ts.sh` from the Claude Code Bash tool (Git Bash).** The Bash tool runs in Git Bash, which automatically converts `/mnt/<drive>/...` paths to Windows paths before passing them to `wsl.exe`. This breaks WSL invocation. Always use:
```bash
MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/path/to/tscodesearch/ts.sh <cmd>
```
`MSYS_NO_PATHCONV=1` disables Git Bash path conversion for that command. `ts.cmd` cannot be invoked from the Bash tool (it requires Windows cmd.exe).

**Line endings: `.sh` files must stay LF.** `.gitattributes` enforces `eol=lf` for `*.sh`, `Dockerfile`, and `*.py`/`*.json`/`*.toml`, and `eol=crlf` for `*.cmd`/`*.bat`. If you ever see a shell script fail with `\r: command not found`, the file has CRLF endings — fix with `git add --renormalize .` (use `-c core.safecrlf=false` if git refuses due to existing mixed-ending files).
