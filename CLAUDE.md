# codesearch — developer notes for Claude

## CRITICAL: no worktrees, no subagents — work serially

**Never use git worktrees or spawn subagents.** Make all edits directly in the main working directory (`Q:\spocore\tscodesearch`). Work step by step.

## CRITICAL: running Python scripts from the Bash tool

**Always use the indexserver WSL venv** for any tscodesearch Python script:

```bash
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/q/spocore/tscodesearch && ~/.local/indexserver-venv/bin/python3 <script> <args>"
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/q/spocore/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ query/tests/ -v"
```

AST debug (no indexserver needed — runs via `.client-venv` on Windows):
```bash
.client-venv\Scripts\python.exe -m query --mode methods --file C:/myproject/src/Widget.cs
```

Management API via curl (read key/port from config.json — never hard-code):
```bash
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String((c.port??8108)+1))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-TYPESENSE-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

## CRITICAL: host-side orchestration scripts must be Node.js

All orchestration scripts invoked from the host = `.mjs`/`.js`. Bash only inside Docker/WSL (`scripts/`). The MCP server is Python (`mcp_server.py`) via `.client-venv\Scripts\python.exe` — that's fine, it has no WSL dependency.

## CRITICAL: fictional names in examples and documentation

Never use real names from the searched codebase (types, methods, namespaces) in docstrings, comments, CLI help, tool descriptions, or examples. Always use fictional generics: `Widget`, `IRepository`, `SaveChanges`, `Order`.

---

## Architecture

```
Windows side
────────────────────────────────────────────────────────────────
  tsquery_server.py (daemon)          mcp_server.py (MCP stdio)
  started by ts start / mcp_server      started by Claude Code
  owns PORT+1                           calls HTTP API at PORT+1
        │◄──────────────── PORT+1 ──────────────────────────────┤
        ├── ThreadingHTTPServer on PORT+1   ← VS Code extension
        ├── watchdog Observer (ReadDirectoryChangesW on Windows)
        ├── IndexQueue worker (batch Typesense writes)
        ├── Syncer (verify/index jobs)
        └── Heartbeat (pings Typesense, restarts on failure)
                  │ TCP localhost:PORT  (WSL2 auto-forwards)
                  ▼
        Typesense  (WSL binary or Docker — Linux only)
```

`tsquery_server.start_daemon()` tries to bind PORT+1; returns `False` if another instance is already running. Management API endpoints: `GET /health`, `GET /status`, `POST /check-ready`, `POST /index/start`, `POST /verify/start`, `POST /verify/stop`, `POST /query-codebase`, `POST /file-events`, `POST /management/shutdown`.

`query_single_file` bypasses HTTP entirely — calls `query_file()` from `query/dispatch.py` in-process. Works without the daemon.

---

## Module map

### Client-side (repo root)

| File | Responsibility |
|------|---------------|
| `tsquery_server.py` | Cross-platform management daemon. HTTP + watcher + heartbeat + syncer threads. Runs under `.client-venv` (Windows) or `indexserver-venv` (Linux/WSL). |
| `mcp_server.py` | FastMCP server. Exposes `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `manage_service`. Calls `tsquery_server.start_daemon()` at startup. `--daemon` flag runs as a standalone daemon. |
| `query/cs.py` | C# AST functions + `query_cs_bytes()`. `EXTENSIONS = frozenset({".cs"})`. |
| `query/py.py` | Python AST functions + `query_py_bytes()`. `EXTENSIONS = frozenset({".py"})`. |
| `query/js.py` | JS/TS AST functions + `query_js_bytes()`. Extensions: `.js .jsx .mjs .cjs .ts .tsx`. |
| `query/rust.py` | Rust AST functions + `query_rust_bytes()`. `EXTENSIONS = frozenset({".rs"})`. |
| `query/cpp.py` | C/C++ AST functions + `query_cpp_bytes()`. Extensions: `.cpp .cc .cxx .c .h .hpp .hxx`. |
| `query/sql.py` | SQL AST functions + `query_sql_bytes()`. `EXTENSIONS = frozenset({".sql"})`. |
| `query/dispatch.py` | Pure query layer. `query_file(src_bytes, ext, mode, mode_arg, ...)`, `describe_file()`, `ALL_EXTS`. No Typesense dependency. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs`. Also JSON stdin mode. |

### Server-side (`indexserver/`)

| File | Responsibility |
|------|---------------|
| `config.py` | Reads `config.json`. `API_PORT = PORT + 1`, `ROOT_EXTENSIONS`, `extensions_for_root()`. |
| `indexer.py` | `run_index()`, `walk_source_files()`, `index_file_list()`, `build_schema()`. |
| `verifier.py` | `run_verify()` (two-phase diff + repair), `check_ready()` (read-only health check). |
| `watcher.py` | `run_watcher()`. Uses `watchdog.observers.Observer` on Windows (real-time), `PollingObserver` on Linux/WSL (10 s poll). |
| `index_queue.py` | Deduplicated batch queue for all Typesense writes. |
| `start_server.py` | Downloads Typesense binary, starts it, writes `typesense.pid`. |
| `service.py` | CLI for Typesense lifecycle: `start`, `stop`, `restart`, `status`, `index`, `verify`, `log`. Called by `ts.mjs` WSL mode. |

### Scripts / infra

| File | Responsibility |
|------|---------------|
| `scripts/entrypoint.sh` | Starts Typesense, waits for health, then exits (WSL `--background`) or keeps alive (Docker foreground). Never starts the management API. |
| `ts.mjs` | Management CLI. WSL mode: calls `service.py start/stop` for Typesense, spawns `tsquery_server.py --daemon` for the management API. Docker mode: `docker start` + same daemon spawn. |
| `setup.mjs` | Creates `.client-venv`, WSL venv, `config.json`, registers MCP. |
| `run_tests.mjs` | VS Code extension unit tests (no Typesense required). |

---

## Entry points

| Command | What it does |
|---------|-------------|
| `ts.cmd <cmd>` | `node ts.mjs %*` |
| `mcp.cmd` | `.client-venv\Scripts\python.exe mcp_server.py` |
| `setup.cmd [--wsl]` | `node setup.mjs` |
| `run_tests.cmd` | `node run_tests.mjs` — VS Code tests |

## Venvs

| Venv | Location | Packages |
|------|----------|----------|
| Client | `.client-venv/` (Windows) | `mcp`, `tree-sitter`, all grammar packages, `typesense`, `watchdog`, `pathspec` |
| Indexserver | `~/.local/indexserver-venv/` (WSL) | `typesense`, `tree_sitter_c_sharp`, `tree_sitter`, `watchdog`, `pathspec`, `pytest` |

## config.json

```json
{
  "api_key": "codesearch-local",
  "mode": "docker",
  "roots": { "default": "C:/myproject/src" }
}
```

`mode` = `"docker"` or `"wsl"`. Roots use Windows paths (`C:/...`). `collection_for_root(name)` → `"codesearch_{sanitized_name}"` (default → `codesearch_default`).

---

## Tool selection guide

| Goal | Tool |
|------|------|
| Exact line-level results across the codebase | `query_codebase` |
| Inspect/enumerate one specific file | `query_single_file` |

**`query_codebase`**: Typesense pre-filter → ≤50 files → tree-sitter exact lines. Returns subsystem breakdown if >50 files match. Pattern-based modes only; listing modes redirect to `query_single_file`. `uses` accepts `uses_kind`: `field`, `param`, `return`, `cast`, `base`.

**`query_single_file`**: No Typesense. Supports listing modes (`methods`, `fields`, `classes`, `usings`, `imports`). Works offline.

## Typesense schema — search mode mapping

| Mode | `query_by` field(s) | Notes |
|------|---------------------|-------|
| `text` | `filename`, `class/method_names`, `content` | Broad keyword |
| `declarations` | `method_sigs`, `method_names`, `filename` | Precise [T1] |
| `implements` | `base_types`, `class_names`, `filename` | Precise [T1] |
| `calls` | `call_sites`, `filename` | Precise [T1] |
| `uses` | `type_refs`, `symbols`, `class_names`, `filename` | Broader [T2] |
| `attrs` | `attributes`, `filename` | Broader [T2] |
| `all_refs` | `type_refs`, `call_sites`, `filename` | Broadest |
| `accesses_on` | `type_refs`, `filename` | Member accesses on type instances |
| `accesses_of` | `call_sites`, `filename` | Access sites of a property/field name |

T1 = precise tree-sitter extractions. T2 = broader, minor false positives possible.

## tree-sitter query modes

### C# (`.cs`)

| Mode | Arg | Finds |
|------|-----|-------|
| `classes`, `methods`, `fields`, `usings` | — | Listing *(query_single_file only)* |
| `params` | METHOD | Parameter list *(query_single_file only)* |
| `text` | NAME | Full source of method/type |
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

Tests are split into three directories:

| Directory | Server needed | Contents |
|-----------|--------------|----------|
| `tests/unit/*.py` | **no** | Unit tests: extractors, queue, verifier diff logic, path translation, mcp_server helpers |
| `tests/integration/*.py` | **yes** | Integration tests: indexer, verifier, watcher, search modes live, sample e2e |
| `query/tests/*.py` | **no** | AST query unit tests for all C# modes and edge cases |

`tests/integration/conftest.py` automatically starts an isolated Typesense on port 18108 (`CODESEARCH_TEST_PORT`) and writes a test config with `root1`/`root2` pointing to `sample/`. Production port 8108 is never touched.

```bash
# Full suite (pytest discovers all three directories)
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/q/spocore/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ query/tests/ -v"

# Filter by test name or class
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/q/spocore/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ -k TestQCasts -v"

# Single file
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/q/spocore/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/unit/test_watcher.py -v"

# VS Code extension tests (no Typesense required)
node run_tests.mjs
```

---

## Common gotchas

**Windows paths.** `config.json` roots are Windows-style (`C:/...`). `mcp_server.py` and `tsquery_server.py` use them directly. WSL code calls `config.to_native_path()` → `/mnt/c/...`. Never convert in Windows-side code.

**Watcher observer selection.** `watcher.py` uses `watchdog.observers.Observer` on Windows (ReadDirectoryChangesW, ~1 s latency) and `PollingObserver` on Linux/WSL (inotify doesn't fire for NTFS `/mnt/` changes). Don't hardcode either.

**`entrypoint.sh` only starts Typesense.** The management API (PORT+1) is always `tsquery_server.py` running on Windows.

**`entrypoint.sh --background` vs `--background --disown`.** Without `--disown`, daemons die when the WSL session ends (intentional for test teardown). With `--disown`, they survive (used by `ts start`).

**Running tests from the Bash tool.** For pytest, use `MSYS_NO_PATHCONV=1 wsl.exe bash -lc "..."` — the Bash tool runs in Git Bash on Windows. For VS Code tests, `node run_tests.mjs` works directly (Git Bash `node` = Windows Node.js). Don't use `wsl.exe node`.

**Line endings.** `.sh` files must be LF. `.gitattributes` enforces this. Fix with `git add --renormalize .` if a script fails with `\r: command not found`.

**`scripts/` vs `docker/`.** `docker/` = `Dockerfile` + `docker-compose.yml` only. All shell scripts are in `scripts/`.
