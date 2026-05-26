# Agent notes

This is the canonical instructions file for repository guidance.
Put all long-form agent instructions in AGENTS.md.
CLAUDE.md is a placeholder that points here.

Operational notes for agents working in this repo are maintained below.

## GitHub workflow preference

This repository is hosted on GitHub.
When working with pull requests, comments, checks, issues, and repository metadata,
prefer GitHub-native tooling (for example, GH CLI and available GitHub-integrated
tools) whenever possible.

## Debugging the indexer: real-time CSV logs

When something goes wrong on the indexer side (the classic symptom: files
get re-indexed on every restart even though the index already contains
them), turn on the CSV debug log.

### Enable

Set `csv_debug` in `config.json`:

```json
{
  "port": 8108,
  "csv_debug": true,
  "roots": { "default": { "path": "q:/spocore/src" } }
}
```

Then `node ts.mjs restart` (or `ts restart` if the cmd is on PATH).

Accepted values:

| value                              | effect                                                          |
|------------------------------------|-----------------------------------------------------------------|
| `false` / `""` / unset             | off                                                             |
| `true` / `"1"` / `"on"` / `"true"` | logs to `<repo>/.tantivy_csv/`                                  |
| any other string                   | treated as an explicit directory path -- logs land there        |

Logging is opt-in and append-only; rows from successive restarts share a
file and are distinguished by the per-row `pid` column.

### CSV files

Written by `indexserver/debug_logger.py`, one file per event type. Each row
starts with `ts` (ms-precision local time) and `pid`.

| file                 | rows are written when                                              | useful columns                                      |
|----------------------|--------------------------------------------------------------------|------------------------------------------------------|
| `session.csv`        | daemon start/stop                                                  | `action` = `start`/`stop`                            |
| `backend_export.csv` | verifier exports the index map at scan start (once per session)    | `doc_id`, `mtime`, `relative_path`                   |
| `fs_walk.csv`        | every file the verifier walks against the exported map             | `decision` = `matched` / `missing` / `stale`         |
| `orphan.csv`         | index entries with no matching file on disk (about to be deleted)  | `doc_id`                                             |
| `enqueue.csv`        | every `IndexQueue.enqueue` (including dedup hits)                  | `action`, `reason`, `is_new`                         |
| `parse.csv`          | every tree-sitter parse the queue worker runs                      | `parse_ms`, `ok`, `error`                            |
| `commit.csv`         | every Tantivy commit, success or failure                           | `duration_ms`, `success`, `error`                    |
| `watcher.csv`        | every watchdog event                                               | `src_path`, `action`                                 |

Decision values in `fs_walk.csv`:

* `matched` -- file's mtime equals the indexed mtime; nothing to do
* `missing` -- file's `doc_id` is not in the index; enqueued as `new`
* `stale`   -- file is in the index but its mtime has changed; enqueued as `modified`

### Scanning the logs

`scripts/scan_csv.py` ingests the CSV directory and prints per-session
breakdowns plus anomaly flags.

```powershell
# default: summary table (one row per restart, anomaly notes on the right)
.client-venv/Scripts/python.exe -m scripts.scan_csv

# list each session with timestamps and counts
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode sessions

# drill into one session
.client-venv/Scripts/python.exe -m scripts.scan_csv --session 1 --mode summary

# top stale files with mtime-delta buckets (was the change recent? batched?)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode stales

# list missing files (filename + on-disk mtime)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode missing

# orphan doc ids with relative_path (joined from the same session's export)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode orphans

# every commit / parse failure with the error string
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode errors

# diff the last two index exports: what disappeared, what appeared, what changed mtime
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode index-diff

# files that flipped between matched and stale/missing across sessions
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode flapping
```

Override the log directory with `--csv-dir DIR` or
`TSCODESEARCH_CSV_DIR=DIR`.

### Anomaly flags emitted by `summary`

* `INDEX_EMPTY_ON_OPEN` -- the index reported 0 docs at scan start; if
  the previous session ended with a populated index this means the
  Tantivy state was lost between restarts (e.g. `meta.json` blown away,
  segments unreachable). Look for prior `commit.csv` failures.
* `ALL_MISSING` -- every walked file is missing from the index. Same
  signal as the empty-index case but observed via the fs walk.
* `COMMIT_FAIL=N` -- Tantivy commit failed N times. The error column in
  `commit.csv` shows the cause (typically Windows `Access is denied`).
* `ORPHAN_HEAVY (X/Y)` -- more than half the index entries didn't match
  any file on disk; check whether the configured root path changed.
* `PARSE_FAIL=N` -- file read / tree-sitter parse failures.

### Disabling

Set `"csv_debug": false` in `config.json` and restart. The files stick
around for offline analysis; delete the `csv/` directory when done.


---

## Migrated Reference (from CLAUDE.md)

# codesearch -- developer notes for Claude

## CRITICAL: ASCII only -- no Unicode in source files

Never introduce non-ASCII characters in code, comments, docstrings, output strings, or docs.
Reason: Windows `cp1252` and downstream tooling can misrender or fail on Unicode.

Use plain ASCII substitutions, for example:
- arrows -> `->`, `<-`, `<->`, `=>`
- long dashes -> `-` or `--`
- ellipsis -> `...`
- bullets/checks/math symbols -> `-`/`*`, `OK`/`NO`, `>=`/`<=`

Validation:
- `python -m scripts.find_nonascii`
- `python -m scripts.replace_nonascii --apply`

## CRITICAL: running Python scripts from the Bash tool

Everything runs in the **client venv** on Windows -- there is no separate WSL venv anymore.

```bash
.client-venv/Scripts/python.exe <script> <args>
.client-venv/Scripts/python.exe -m pytest tests/ query/tests/ -v
```

AST debug:
```bash
.client-venv/Scripts/python.exe -m query --mode methods --file C:/myproject/src/Widget.cs
```

Management API via curl (read key/port from config.json -- never hard-code):
```bash
API_KEY=$(node -e "const c=require('./config.json'); process.stdout.write(c.api_key)")
API_PORT=$(node -e "const c=require('./config.json'); process.stdout.write(String(c.port??8108))")
curl -s -X POST http://localhost:$API_PORT/query-codebase \
  -H "Content-Type: application/json" -H "X-API-KEY: $API_KEY" \
  -d '{"mode":"declarations","pattern":"SaveChanges","root":""}' | python -m json.tool
```

The daemon authenticates every request by matching the `X-API-KEY` header against `config.json`'s `api_key`. The daemon's HTTP server binds `localhost` only, but the key still matters: any process on the same machine (browser background pages, other dev tools, a malicious dependency) can reach `localhost:PORT`. Requiring a shared secret means a random local process can't query or mutate the index without first reading `config.json`.

## CRITICAL: host-side orchestration scripts must be Node.js

All orchestration scripts invoked from the host = `.mjs`/`.js`. The MCP server is Python (`mcp_server.py`) via `.client-venv\Scripts\python.exe`.

## CRITICAL: fictional names in examples and documentation

Never use real names from the searched codebase (types, methods, namespaces) in docstrings, comments, CLI help, tool descriptions, or examples. Always use fictional generics: `Widget`, `IRepository`, `SaveChanges`, `Order`.

---

## Architecture

```
Windows side
----------------------------------------------------------------
  indexserver/daemon.py (daemon)      mcp_server.py (MCP stdio)
  started by ts start                  started by Claude Code
  owns the management API port         calls HTTP API at port
        |<---------------- port ---------------------------------|
        |--- ThreadingHTTPServer on PORT    <- VS Code extension
        |--- watchdog Observer (ReadDirectoryChangesW on Windows)
        |--- IndexQueue worker (batch Tantivy writes)
        |--- Syncer (verify/index jobs)
        |--- pystray tray icon (Windows -- magnifying glass)
        `--- Tantivy backends (one per root, on-disk in <repo>/.tantivy/)
```

`start_daemon()` in `indexserver/daemon.py` tries to bind PORT; returns `False` if another instance is already running. There is no separate Typesense process: the search index lives in-process via `tantivy-py`. On Windows the daemon calls `FreeConsole()` at startup so no console window appears.

Management API endpoints: `GET /health`, `GET /status`, `POST /check-ready`, `POST /verify/start`, `POST /verify/stop`, `POST /query-codebase`, `POST /file-events`, `POST /management/shutdown`.

`query_single_file` bypasses HTTP entirely -- calls `query_file()` from `query/dispatch.py` in-process. Works without the daemon.

---

## Module map

### Backend (search index)

| File | Responsibility |
|------|---------------|
| `indexserver/backend.py` | Tantivy schema definition + `Backend` (write/read/upsert/delete/export). One Tantivy index per "collection"; on-disk directory `<repo>/.tantivy/<collection>/`. |
| `indexserver/search.py` | `search()` -- Typesense-shaped result dict on top of `Backend`. Translates `query_by`/`weights`/`num_typos`/`filter_by` into Tantivy queries. |

### Daemon + MCP

| File | Responsibility |
|------|---------------|
| `indexserver/daemon.py` | Cross-platform management daemon. HTTP + watcher + queue + syncer threads + pystray tray icon. Opens one `Backend` per root at startup. Calls `FreeConsole()` on Windows so no console window appears. |
| `mcp_server.py` | FastMCP server. Exposes `query_codebase`, `query_single_file`, `ready`, `verify_index`, `service_status`, `wait_for_sync`. Auto-starts the daemon on first tool call. |

### Query (AST)

| File | Responsibility |
|------|---------------|
| `query/cs.py`, `query/py.py`, `query/js.py`, `query/rust.py`, `query/cpp.py`, `query/sql.py` | Per-language tree-sitter AST functions and bytes-level mode handlers. |
| `query/_util.py` | Shared dataclasses (`FileDescription`, `ClassInfo`, ...), `TreeIndex` (single-pass AST walker used by every language), and `_run_dispatch` (resolves a mode against a language's dispatch table; handles the `capabilities` mode and raises `ValueError` with the supported-mode list for unknown modes). |
| `query/dispatch.py` | Pure query layer. `query_file(src_bytes, ext, mode, mode_arg, ...)`, `describe_file()`, `ALL_EXTS`. No backend dependency. `query_file` raises `ValueError` for unknown extensions or unsupported modes -- explicit errors beat silent empties for tool-using agents. |
| `query/__main__.py` | CLI: `python -m query --mode methods --file Widget.cs`. JSON stdin mode also supported. |

**`TreeIndex`** (in `query/_util.py`) walks the AST once with tree-sitter's C-level `TreeCursor`, buckets nodes into `nodes_by_type[type] -> [nodes]`, and optionally collects literal-aware `all_refs` in the same pass. `describe_*_file` builds one index covering the union of types every extractor needs (5-10x faster than a per-extractor walk on large files); per-query wrappers (`q_classes`, `q_methods`, ...) build a narrow index with just their target types so they pay the same cost as one targeted walk.

### Indexer

| File | Responsibility |
|------|---------------|
| `query/config.py` | Reads `config.json`. `Config`, `Root`, `load_config()`, `collection_for_root()`, `INCLUDE_EXTENSIONS`. |
| `indexserver/indexer.py` | `run_index()`, `walk_source_files()`, `index_file_list()`, `ensure_backend()`. |
| `indexserver/verifier.py` | `run_verify()` (two-phase diff + repair), `check_ready()` (read-only health check). |
| `indexserver/watcher.py` | `run_watcher()`. `watchdog.observers.Observer` on Windows (real-time), `PollingObserver` on Linux/WSL. |
| `indexserver/index_queue.py` | Deduplicated batch queue. Writes go through a `BackendResolver` (`collection_name -> Backend`). |
| `indexserver/query_util.py` | Structural query CLI (`python -m indexserver.query_util ...`). `--search` opens the backend in read-only mode. |

### Scripts

| File | Responsibility |
|------|---------------|
| `scripts/search.py` | Standalone search CLI. Opens a read-only `Backend` and calls `indexserver.search.search()`. |
| `scripts/parse_perf.py` | Profile parsing of one file: wall-clock + cProfile breakdown of `describe_file`. Useful for diagnosing slow files in the indexer. Usage: `python -m scripts.parse_perf <file> [--top N] [--runs N]`. |
| `ts.mjs` | Daemon CLI: `start`/`stop`/`restart`/`status`/`index`/`verify`/`log`/`root`. Spawns `indexserver.daemon` and posts to its API. |
| `setup.mjs` | One-time setup: `.client-venv`, `config.json` (prompts for root dir), MCP registration (Claude Code + VS Code/GitHub Copilot), VS Code extension. |
| `run_tests.mjs` | VS Code extension unit tests (no daemon required). |

---

## Entry points

| Command | What it does |
|---------|-------------|
| `ts.cmd <cmd>` | `node ts.mjs %*` |
| `mcp.cmd` | `.client-venv\Scripts\python.exe mcp_server.py` |
| `setup.cmd` | `node setup.mjs` |
| `run_tests.cmd` | `node run_tests.mjs` -- VS Code tests |

## Venvs

| Venv | Location | Packages |
|------|----------|----------|
| Client | `.client-venv/` (Windows) | `mcp`, `tree-sitter`, all grammar packages, `tantivy`, `watchdog`, `pathspec`, `pytest` |

There is **no longer a WSL or indexserver venv** -- Tantivy runs in-process in the same `.client-venv` Python.

## config.json

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": { "default": { "path": "C:/myproject/src" } }
}
```

`port` is the daemon's HTTP API port (single port, no Typesense+1). Roots use Windows paths (`C:/...`). Root entries can be either `{"path": "..."}` objects (what `setup.mjs` and `ts root --add` write) or bare strings (`"C:/..."`); both are parsed by `_parse_roots` in `query/config.py`. `collection_for_root(name)` -> `"codesearch_{sanitized_name}"` (default -> `codesearch_default`). Each collection's index lives at `<repo>/.tantivy/<collection>/`.

---

## Tool selection guide

MCP tools are invoked with the full namespaced name `mcp__tscodesearch__<name>` (e.g. `mcp__tscodesearch__query_codebase`). Shortening to `mcp__tscodesearch` returns "No such tool available" -- the suffix is required.

| Goal | Tool |
|------|------|
| Exact line-level results across the codebase | `query_codebase` |
| Inspect/enumerate one specific file | `query_single_file` |

**`query_codebase`**: Tantivy pre-filter -> <=50 files -> tree-sitter exact lines. Returns folder breakdown (one level deeper than the current `sub=` scope) if >50 files match. Pattern-based modes only; listing modes redirect to `query_single_file`. `uses` accepts `uses_kind`: `field`, `param`, `return`, `cast`, `base`. `sub=` accepts any folder depth (`services` or `services/billing`).

**`query_single_file`**: No backend search. Supports listing modes (`methods`, `fields`, `classes`, `imports`, `capabilities`). Works offline.

## Backend schema -- search mode mapping

The daemon ignores any caller-supplied `query_by`/`weights` for `/query-codebase` and resolves them server-side from the mode (see `_resolve_query_params` in `indexserver/daemon.py`).

| Mode | `query_by` field(s) | Notes |
|------|---------------------|-------|
| `declarations` (default) | `class_names`, `method_names`, `path_tokens` | [T1] -- narrowed by `symbol_kind`: type kinds -> `class_names,path_tokens`; member kinds -> `method_names,path_tokens` |
| `implements` | `base_types`, `class_names`, `path_tokens` | [T1] |
| `calls` | `call_sites`, `qualified_calls`, `path_tokens` | [T1] -- bare `Save` hits `call_sites`; qualified `IRepository.Save` / `Foo.Save` hits `qualified_calls` |
| `uses` (default) | `type_refs`, `cast_types`, `path_tokens` | [T2] -- narrowed by `uses_kind` (see below) |
| `uses` `uses_kind=field` | `field_types`, `path_tokens` | [T1] |
| `uses` `uses_kind=param` | `param_types`, `path_tokens` | [T1] |
| `uses` `uses_kind=return` | `return_types`, `path_tokens` | [T1] |
| `uses` `uses_kind=cast` | `cast_types`, `path_tokens` | [T1] |
| `uses` `uses_kind=base` | `base_types`, `class_names`, `path_tokens` | [T1] |
| `uses` `uses_kind=locals` | `local_types`, `path_tokens` | [T1] |
| `casts` | `cast_types`, `path_tokens` | [T1] |
| `attrs` | `attr_names`, `path_tokens` | [T1] |
| `accesses_on` | `type_refs`, `cast_types`, `path_tokens` | Shares the `uses` resolver, then AST narrows to `.Member` accesses on instances of the type |
| `accesses_of` | `member_accesses`, `path_tokens` | [T1] |
| `all_refs` | `path_tokens`, `class_names`, `method_names`, `tokens` | Broadest -- index pre-filter only; AST then matches every identifier occurrence |

T1 = precise tree-sitter extractions. T2 = broader, minor false positives possible (AST post-filter then drops index false positives).

### Tokenizer and storage choices

Every text field uses the **`raw`** tokenizer: each entry is one verbatim term (case-sensitive, no underscore splitting, no length limit). The indexer does all domain-aware splitting before storing -- long identifiers like `InitializeNotificationHistoryAcrossDataCentersUSA` stay whole, `add_text_field` is one token, and `Acme.Billing.Service` is stored as three `namespace` entries (`Acme`, `Billing`, `Service`).

Only a small set of fields is `stored=True` (retrievable from the index at search time):

| Stored field | Purpose |
|--------------|---------|
| `id` | Primary key for upsert/delete. |
| `relative_path` | Returned to every caller so they know which file matched. |
| `filename` | Basename (e.g. `Foo.cs`) -- used for display. |
| `extension`, `language` | Used as exact-match filter terms (`extension:=cs`) and for status output. |
| `path_segments` | Cumulative ancestor folders for the `sub=` filter (`["services", "services/billing"]`). |
| `mtime` | Read by the verifier to diff fs vs. index. |

Every other text field -- `class_names`, `method_names`, `base_types`, `field_types`, `local_types`, `param_types`, `return_types`, `cast_types`, `type_refs`, `call_sites`, `qualified_calls`, `member_accesses`, `attr_names`, `imports`, `namespace`, `tokens`, `path_tokens`, `member_sig_tokens` -- is `stored=False`. The fields are indexed for search but never read back from the index. The daemon's pipeline pre-filters with Tantivy then runs tree-sitter on the candidate files, and the AST output is what carries line-level results all the way to the caller.

`path_tokens` collects every directory name plus the filename, the filename stem, and the extension for one file -- so a search for `billing` finds every file under any `billing/` directory at any depth, and a search for `Foo` matches `Foo.cs`. `namespace` is a multi-value field; the indexer splits on `.` for C#/Python/Java/JS (other languages can return a pre-split list from their extractor). `member_sig_tokens` is every identifier appearing in any member signature -- attribute names, parameter names, generic args, default-value identifiers -- collected by each language's AST extractor walking the member node and skipping the body. The legacy `member_sigs` (full signature strings) field is gone; the structured fields (`method_names`, `return_types`, `param_types`, `class_names`) plus `member_sig_tokens` cover the same searches.

`tokens` is the per-file deduped bag of every identifier -- identifiers inside string literals, char literals, and comments are excluded.

`qualified_calls` carries `Type.Method` tokens for call sites whose receiver type is *syntactically obvious*: PascalCase identifier receivers (`Foo.Save()` -> `Foo.Save` -- captures both static class calls and PascalCase locals literally) and receivers whose declared/inferred type was pinned by the per-file var-type map (`repo.Save()` where `repo: IRepository` -> `IRepository.Save`). The map is method-scoped and conflict-suppressing: a name with two distinct types in one method's scope (e.g. shadowed across `if`/`else` branches) emits no qualified form for that call. Cases the map *doesn't* resolve -- `var x = GetThing()`, generic inference, LINQ lambdas without typed params -- leave the qualified form absent; the bare name in `call_sites` still finds the call. Agents that know the receiver's type query the qualified form for precision; agents that don't fall back to the bare method name.

## tree-sitter query modes

**One canonical mode name per concept across every language.** The dispatch raises a `ValueError` listing the supported modes when an unknown one is passed. Call `query_single_file("capabilities", file=...)` to enumerate the modes a given file's language actually supports.

| Mode | Arg | Concept | Languages |
|------|-----|---------|-----------|
| `capabilities` | -- | List the modes supported for this file's language | all |
| `classes` | -- | Type declarations (class/interface/struct/enum/record/...) | all |
| `methods` | -- | Method/ctor/property/field/event declarations | all |
| `fields` | -- | Field / property / column declarations | C#, SQL |
| `imports` | -- | `using` / `import` / `include` directives | all except SQL |
| `params` | METHOD | Parameter list for METHOD | C#, Python, JS, Rust, C++ |
| `declarations` | NAME | The declaration(s) of NAME (narrow with `symbol_kind`) | all |
| `body` | NAME | Full source of NAME's declaration(s). Works in both `query_codebase` (returns bodies across every matching file) and `query_single_file` (one file only). Narrow with `symbol_kind`. | C# only |
| `at` | LINE:COL | Deepest AST node at position + enclosing scope chain | C# only |
| `calls` | METHOD | Call sites of METHOD. Qualify with `Type.Method` to restrict by receiver -- the qualifier matches both the literal receiver text (`Foo.Save()`) **and** any receiver whose declared/inferred type resolves to that name via the method-scoped var-type map (`store.Save()` where `store: IRepository` matches `IRepository.Save`). When the receiver's type is conflicted in its scope, the qualified match is skipped -- the bare name still finds the call. Pass a METHOD name only -- a variable/receiver name silently returns empty; use `all_refs` on the variable for that. | all |
| `caller_of` | METHOD | Like `calls`, but groups call sites by the **enclosing caller** -- one row per `(TypeName.MemberName)` caller with a count of how many sites it contains. Collapses noisy `calls METHOD` output into a unique-caller view. Useful for "who depends on this". | C# only |
| `callee_of` | METHOD | The inverse -- walk the body of the method named METHOD and emit one row per distinct callee with an invocation count. Constructor calls (`new T()`) are reported as `T (N invocations, ctor)`. Useful for "what does this method depend on" / "what could be slow here". | C# only |
| `implements` | TYPE | Types that inherit/implement TYPE | all except SQL |
| `uses` | TYPE | Type references; narrow with `uses_kind` (`field`/`param`/`return`/`cast`/`base`/`locals`) | C# only |
| `casts` | TYPE | `(TYPE)expr` / `as TYPE` sites | C# only |
| `attrs` | NAME? | `[Attribute]` / `@decorator` / `#[attribute]` usages (omit NAME to list all) | C#, Python, JS |
| `accesses_of` | MEMBER | Access sites of property/field by name (`"Order.Status"` restricts) | C# only |
| `accesses_on` | TYPE | `.Member` accesses on locals/params/fields typed as TYPE (plus `new T { ... }` and `with` mutations). Returns nothing when the variable is only assigned, returned, or forwarded as an argument -- no `.Member` exists. Fall back to `all_refs` on the variable name. | C# only |
| `all_refs` | NAME | Every identifier occurrence (broadest -- AST-only, skips strings/comments). For SQL this is a plain substring scan over lines. | all |
| `var_type` | NAME | For each occurrence of NAME, report the resolved type from the method-scoped var-type map, or `(unresolved)` / `(conflicting)` when the resolver can't pin it down. Works in both `query_codebase` (every file that mentions NAME) and `query_single_file`. Saves an `at LINE:COL` round-trip when you just want the type. | C# only |

**Enclosing-scope filter (pattern modes).** `calls`, `uses`, `casts`, `accesses_of`, `accesses_on`, `all_refs` accept `enclosing_method="WriteBack"` and/or `enclosing_class="OrderProcessor"`. The two compose as a logical AND. Useful for pinpointing call sites in a specific member, e.g. `calls("Save", enclosing_method="WriteBack")` returns only the `Save()` calls that happen inside `WriteBack` methods. (C# only.)

**Visibility filter (declaration modes).** `declarations`, `classes`, `methods`, `fields` accept `visibility="public,internal,protected,private"` (comma-separated, any subset). The filter is applied AST-side per declaration using the same defaults the indexer uses: top-level types default to `internal`, nested types to `private`, interface members to `public`, enum members to `public`, class/struct/record members to `private`. Compound modifiers collapse to their dominant role (`protected internal` -> `protected`, `private protected` -> `private`). Languages other than C# currently don't capture visibility -- passing the filter against them returns nothing rather than over-matching.

---

## Testing

| Directory | Backend needed | Contents |
|-----------|----------------|----------|
| `tests/unit/*.py` | **no** (uses `_FakeBackend`) | Unit tests: extractors, queue, verifier diff, path translation, mcp_server helpers |
| `tests/integration/*.py` | **yes** | Integration tests: indexer, verifier, watcher, search modes live, sample e2e -- each opens a fresh Tantivy index in `<repo>/.tantivy/` |
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

**Tantivy is single-writer.** The daemon owns one `IndexWriter` per collection. CLIs (`scripts/search.py`, `indexserver.query_util --search`) open the index read-only via `ensure_backend(..., write=False)`. Trying to open a writer while the daemon already has one will block or fail -- let the daemon do the writing and search via the HTTP API.

**Index location.** `<repo>/.tantivy/<collection>/`. Wipe with `ts recreate` (which stops the daemon, removes the directory, and restarts).

**Line endings.** `.gitattributes` enforces LF for shell scripts. Fix with `git add --renormalize .` if any cross-OS quoting goes wrong.
