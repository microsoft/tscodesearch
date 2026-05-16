# vscode-codesearch — developer notes for Claude

VS Code extension that provides a real-time search panel backed by the local
Tantivy index managed by the `codesearch/` daemon. As the user types, it
issues queries against the daemon's HTTP API and renders results with file
metadata, highlights, and one-click file opening.

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  WEBVIEW  (HTML + inline JS, runs in isolated iframe)  │
│  search input · mode selector · filters · results list │
│  communicates via postMessage ↕                        │
└──────────────────────────┬─────────────────────────────┘
                           │ vscode.postMessage / onDidReceiveMessage
┌──────────────────────────▼─────────────────────────────┐
│  EXTENSION HOST  (Node.js, src/extension.ts)           │
│  command registration · panel lifecycle                │
│  config discovery · message routing                    │
│       ↓ imports                                        │
│  src/client.ts  (pure logic, no vscode dependency)     │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTP  localhost:{port}
                  codesearch daemon (tsquery_server.py)
                  (in-process Tantivy backend per root)
```

The split between `client.ts` and `extension.ts` is intentional: `client.ts`
imports nothing from `vscode`, so it can be unit-tested with plain Node.js.

## Module map

| File | Responsibility |
|------|---------------|
| `src/client.ts` | All VS Code-free logic. `MODES` constant, `CodesearchConfig` type, `loadConfig`, `getRoots`, `sanitizeName`, `collectionForRoot`, `doQueryCodebase`, `doQuerySingleFile`, `runSearchPipeline`, `renderTextTree`, `resolveFilePath`. Exported for tests. |
| `src/server.ts` | `ServerManager` — daemon lifecycle wrapper. Spawns `node ts.mjs` for start/stop/restart, manages roots in `config.json`. |
| `src/extension.ts` | VS Code wiring only. `friendlyConfigError`, `findConfigPath` (uses `vscode.workspace`), `buildWebviewHtml` (large HTML template literal), `activate` / `deactivate`. |
| `src/status.ts` | Status bar item. Polls the daemon's `/status` and `/verify/status` every 5 s and renders state. |
| `src/treeview.ts` | "Roots" tree view in the activity bar. |
| `src/watcher.ts` | Thin HTTP helper used by the status bar. |
| `src/test/client.test.ts` | ~65 unit tests. No daemon required. Covers config parsing, root/collection resolution, every search mode, path resolution, HTTP behaviour against a mock server. |
| `src/test/pipeline.test.ts` | Pipeline integration tests. Skip when daemon is not running. |
| `src/test/status.test.ts` | Status-bar formatting tests. |
| `package.json` | Extension manifest. Registers `codesearch.openPanel` (Ctrl+Shift+F1), settings, build/test scripts. |
| `tsconfig.json` | CommonJS target, `esModuleInterop: true`, strict. Output to `out/`. |
| `.vscodeignore` | Excludes `src/`, `node_modules/`, maps from the packaged VSIX. |
| `.vscode/launch.json` | F5 launches the Extension Development Host with this folder loaded. |
| `.vscode/tasks.json` | Default build task runs `npm run compile`; background task runs `npm run watch`. |

## Search modes

Defined in `client.ts` `MODES` array. Each entry has `key`, `label`, `queryBy`, `weights`, `desc`, and optionally `astMode`/`uses_kind`. The daemon's `/query-codebase` does an index pre-filter (Tantivy) followed by tree-sitter AST post-processing for line-level matches.

The `queryBy`/`weights` fields on `MODES` are **not** sent to the daemon — `/query-codebase` resolves them server-side from `astMode` + `uses_kind` (see `_resolve_query_params` in `tsquery_server.py`). They survive on the client only as labels for the panel UI and may go away in a future cleanup. The intent column below describes what the user sees; the actual field set queried is determined by the server.

| Key | `astMode` sent to daemon | `uses_kind` | Intent |
|-----|--------------------------|-------------|--------|
| `text` | (key is sent as-is — falls through to `all_refs` server-side) | — | Broad identifier search |
| `declarations` | `declarations` | — | Method/type signature search [T1] |
| `implements` | `implements` | — | Interface implementors [T1] |
| `calls` | `calls` | — | Call sites of a method [T1] |
| `uses` | `uses` | — | Type reference search (default `type_refs,cast_types`) [T2] |
| `casts` | `casts` | — | Explicit `(T)expr` / `as T` cast sites [T1] |
| `attrs` | `attrs` | — | `[Attribute]` decoration [T1] |
| `all_refs` | `all_refs` | — | All identifier occurrences — broadest |
| `accesses_on` | `accesses_on` | — | `.Member` accesses on instances of a type |
| `uses_field` | `uses` | `field` | Fields/properties declared as a given type |
| `uses_param` | `uses` | `param` | Method/ctor parameters typed as a given type |

The server-side field-set mapping (one source of truth) lives in `tsquery_server._resolve_query_params`; see `../CLAUDE.md` for the full mode → query-by table.

## Config

The extension reads `tscodesearch/config.json` — the same file used by the daemon.

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": { "default": { "path": "C:/myproject/src" } }
}
```

**Discovery order:**
1. VS Code setting `tscodesearch.configPath` (must point to the `.json` file, not the directory)
2. Auto-detect: looks for `config.json` in each workspace folder; validates that it contains `api_key` and `roots`

`findConfigPath()` in `extension.ts` validates the configured path explicitly and throws a descriptive error for the three common failure modes:
- Path is a directory → tells the user to point to the file
- Path doesn't exist → tells the user to check the setting
- File contains invalid JSON → tells the user to check for syntax errors

All error notifications include an **Open Settings** button.

## Build

```
cd vscode-codesearch
npm install          # first time only
npm run compile      # one-shot build → out/
npm run watch        # incremental rebuild on save
npm test             # all tests (skip pipeline if daemon not up)
```

The `out/` directory must exist before VS Code loads the extension. If you see "command 'codesearch.openPanel' not found", run `npm run compile` and reload the window.

## Install / run

**Development (F5):**
Open `vscode-codesearch/` as a VS Code workspace, press **F5**. A new Extension Development Host opens with the extension loaded.

**Install from folder (no packaging):**
F1 → `Developer: Install Extension from Location…` → select `vscode-codesearch/`.

**Package and install:**
```
npm install -g @vscode/vsce
npm run package -- -o codesearch.vsix
code --install-extension codesearch.vsix
```

After install, reload the VS Code window. The panel opens with **Ctrl+Shift+F1** or F1 → `TsCodeSearch: Open Panel`.

## Testing

```
npm test                                                # all tests
node --require tsx/cjs --test src/test/client.test.ts   # unit tests only
```

Pipeline tests need the daemon running (`ts start`); they skip with a clear message otherwise.

## Webview message protocol

| Direction | Message | Fields |
|-----------|---------|--------|
| Webview → Host | `search` | `query`, `mode`, `ext`, `sub`, `root`, `limit` |
| Webview → Host | `openFile` | `relativePath`, `root` |
| Host → Webview | `results` | `hits`, `found`, `elapsed`, `query`, `mode` |
| Host → Webview | `error` | `message` |
| Host → Webview | `configError` | `message` |

## Key gotchas

**Must compile before loading.** VS Code loads `out/extension.js`. If `out/` is missing, the command is registered but activation fails silently → "command not found". Always run `npm run compile` (or use `npm run watch` during development).

**Schema is fixed at index creation time.** Indexed fields are populated by the AST extractors, which pre-split compound forms into individual identifiers (e.g. `Task<Widget>` → `Task`, `Widget`). After any schema change, `ts recreate` is the way to rebuild.

**`configPath` must point to the file, not the folder.** The setting expects a path ending in `config.json`. Setting it to the parent directory causes an `EISDIR` error, which `friendlyConfigError()` translates to a readable message.

**Webview HTML is a TypeScript template literal.** The entire HTML including inline JS is built as a backtick string in `buildWebviewHtml()`. The inline JS must use regular quote strings (`'...'`, `"..."`), not template literals, to avoid ambiguity with TypeScript's own `${}` interpolation. The `nonce` and initial data (`MODES`, `ROOTS`, `DEFAULT_ROOT`) are the only TypeScript interpolations.
