# vscode-codesearch — developer notes for Claude

VS Code extension that provides a real-time search panel backed by the Typesense index managed by the `codesearch/` MCP. As the user types, it issues prefix-search queries against Typesense and renders results with file metadata, highlights, and one-click file opening.

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
│  loadConfig · buildSearchParams · tsSearch · doSearch  │
│  resolveFilePath · getRoots · collectionForRoot        │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTP  localhost:{port}
                    Typesense server
              (managed by codesearch/indexserver/)
```

The split between `client.ts` and `extension.ts` is intentional: `client.ts` imports nothing from `vscode`, so it can be unit-tested with plain Node.js.

## Module map

| File | Responsibility |
|------|---------------|
| `src/client.ts` | All VS Code-free logic. `MODES` constant, `CodesearchConfig` / `TypesenseHit` / `TypesenseResult` types, `loadConfig`, `getRoots`, `sanitizeName`, `collectionForRoot`, `buildSearchParams`, `tsSearch`, `doSearch`, `resolveFilePath`. Exported for tests. |
| `src/extension.ts` | VS Code wiring only. `friendlyConfigError`, `findConfigPath` (uses `vscode.workspace`), `buildWebviewHtml` (large HTML template literal), `activate` / `deactivate`. Imports everything search-related from `client.ts`. |
| `src/test/client.test.ts` | 60 unit tests. No server required. Covers config parsing, root/collection resolution, every search mode's `query_by`, filter combinations, typo tolerance, path resolution, HTTP client behaviour (mock server), and `doSearch` end-to-end with mock. |
| `src/test/integration.test.ts` | 23 integration tests. Requires Typesense running (`ts start`). Creates an isolated `codesearch_tstest_{timestamp}` collection, upserts a realistic `WidgetService.cs` document, polls until indexed, then exercises all modes plus extension/subsystem filters. Cleans up on completion. |
| `package.json` | Extension manifest. Registers `codesearch.openPanel` command (Ctrl+Shift+F1), `codesearch.configPath` setting, build/test scripts. |
| `tsconfig.json` | CommonJS target, `esModuleInterop: true`, strict. Output to `out/`. |
| `.vscodeignore` | Excludes `src/`, `node_modules/`, maps from the packaged VSIX. |
| `.vscode/launch.json` | F5 launches the Extension Development Host with this folder as the extension under development. |
| `.vscode/tasks.json` | Default build task runs `npm run compile`; background task runs `npm run watch`. |

## Search modes

Defined in `client.ts` `MODES` array. Each entry has `key`, `label`, `queryBy` (Typesense `query_by` param), `weights`, and `desc`.

| Key | `query_by` fields | Intent |
|-----|-------------------|--------|
| `text` | filename, symbols, class_names, method_names, content | Broad full-text |
| `declarations` | method_sigs, method_names, filename | Method/type signature search [T1] |
| `implements` | base_types, class_names, filename | Interface implementors [T1] |
| `calls` | call_sites, filename | Call sites of a method [T1] |
| `uses` | type_refs, symbols, class_names, filename | Type reference search [T2] |
| `casts` | cast_sites, filename | Explicit cast sites [T2] |
| `attrs` | attributes, filename | Attribute decoration [T2] |
| `all_refs` | type_refs, call_sites, filename | All references — broad, catches everything |
| `accesses_on` | type_refs, filename | Member accesses on instances of a type |
| `uses_field` | type_refs, filename | Fields/properties declared as a given type |
| `uses_param` | type_refs, filename | Method parameters typed as a given type |

## Config

The extension reads `codesearch/config.json` — the same file used by the MCP and indexserver.

```json
{
  "api_key": "codesearch-local",
  "port": 8108,
  "roots": { "default": { "windows_path": "C:/myproject/src" } }
}
```

**Discovery order:**
1. VS Code setting `codesearch.configPath` (must point to the `.json` file, not the directory)
2. Auto-detect: looks for `codesearch/config.json` then `config.json` in each workspace folder; validates that the file contains `api_key` and `roots`/`src_root`

`findConfigPath()` in `extension.ts` validates the configured path explicitly and throws a descriptive error (caught by `reloadConfig()` and mapped to a human-readable notification via `friendlyConfigError()`) for the three common failure modes:
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
npm test             # unit + integration tests
```

The `out/` directory must exist before VS Code loads the extension. If you see "command 'codesearch.openPanel' not found", run `npm run compile` and reload the window.

## Install / run

**Development (F5):**
Open `vscode-codesearch/` as a VS Code workspace, press **F5**. A new Extension Development Host window opens with the extension loaded.

**Install from folder (no packaging):**
F1 → `Developer: Install Extension from Location…` → select `vscode-codesearch/`.

**Package and install:**
```
npm install -g @vscode/vsce   # once
setup-vscode-ext.cmd
```
`setup-vscode-ext.cmd` runs `npm install`, `npm run compile`, `vsce package`, and `code --install-extension`.

After any of the above, reload the VS Code window. The panel opens with **Ctrl+Shift+F1** or F1 → `Code Search: Open Panel`.

## Testing

```
npm test
```

Runs both test files sequentially via `node --require tsx/cjs --test`. The integration tests auto-skip with a clear message if Typesense is not running.

```
# Unit tests only (always runnable):
node --require tsx/cjs --test src/test/client.test.ts

# Integration tests only (requires: ts start):
node --require tsx/cjs --test src/test/integration.test.ts
```

The integration test creates a fresh `codesearch_tstest_{timestamp}` collection so it never interferes with the real index. It always deletes the collection in the `after` hook, even if tests fail.

## Webview message protocol

| Direction | Message | Fields |
|-----------|---------|--------|
| Webview → Host | `search` | `query`, `mode`, `ext`, `sub`, `root`, `limit` |
| Webview → Host | `openFile` | `relativePath`, `root` |
| Host → Webview | `results` | `hits`, `found`, `elapsed`, `query`, `mode` |
| Host → Webview | `error` | `message` |
| Host → Webview | `configError` | `message` |

## File watcher

The Windows file system watcher lives in `src/watcher.ts` (`FileWatcher` class). It uses VS Code's native `createFileSystemWatcher` (backed by `ReadDirectoryChangesW`) to detect changes under Windows-path roots, then forwards batched events to the indexserver API at `POST /file-events`.

On startup it:
1. Calls `POST /watcher/pause` to stop the WSL `PollingObserver` (so changes aren't double-processed)
2. Calls `POST /verify/start` for each root to catch up on changes while VS Code was closed
3. Creates a `vscode.FileSystemWatcher` per root and logs `[watcher] Windows watcher activated for root "…" (…)` to the output channel

All startup logging (including errors) goes to the TsCodeSearch output channel. The `_start()` method is `async` and wraps its body in try/catch so errors are visible rather than silently swallowed.

On disposal it calls `POST /watcher/resume` so the WSL `PollingObserver` takes over again.

Only roots whose path matches `WIN_PATH_RE` (`/^[A-Za-z]:[/\\]/`) are watched by this class; Linux/WSL paths are left to the indexserver's polling.

## Key gotchas

**Must compile before loading.** VS Code loads `out/extension.js`. If `out/` is missing, the command is registered but the activation fails silently → "command not found". Always run `npm run compile` (or use `npm run watch` during development).

**`token_separators` requires a re-index.** The Typesense schema includes `token_separators: ["(", ")", "<", ">", "[", "]", ","]` so that parameter types and generic type arguments are individually tokenisable (e.g. searching `int` finds `GetAsync(int id)`). This is a collection-level setting that can only be applied at creation time. After any schema change, run `ts index --reset`.

**`configPath` must point to the file, not the folder.** The setting `codesearch.configPath` expects a path ending in `config.json`, not the parent directory. Setting it to the `codesearch/` directory causes an `EISDIR` error, which `friendlyConfigError()` translates to a readable message.

**Webview HTML is a TypeScript template literal.** The entire HTML including inline JS is built as a backtick string in `buildWebviewHtml()`. The inline JS must use regular quote strings (`'...'`, `"..."`), not template literals, to avoid ambiguity with TypeScript's own `${}` interpolation. The `nonce` and initial data (`MODES`, `ROOTS`, `DEFAULT_ROOT`) are the only TypeScript interpolations.

**Path resolution for WSL roots.** `resolveFilePath()` in `client.ts` converts `/mnt/<drive>/...` WSL paths to `<Drive>:/...` Windows paths before calling `vscode.Uri.file()`, because the extension host runs on Windows even when Typesense is running in WSL.
