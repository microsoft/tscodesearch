import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

import {
    CodesearchConfig,
    MatchItem,
    loadConfig,
    getRoots,
    runSearchPipeline,
    doQuerySingleFile,
    resolveFilePath,
} from './client';
import { buildWebviewHtml, getNonce } from './webview';
import { FileWatcher } from './watcher';
import { StatusBarManager } from './status';
import { ServerManager } from './server';
import { RootsTreeProvider, CodesearchTreeItem } from './treeview';

// ---------------------------------------------------------------------------
// Config discovery (needs vscode API)
// ---------------------------------------------------------------------------

/** Returns the path to config.json at <repoPath>/config.json, or null if not found. */
function findConfigPath(repoPath: string): string | null {
    if (!repoPath) { return null; }
    const candidate = path.join(repoPath, 'config.json');
    if (!fs.existsSync(candidate)) { return null; }
    try {
        const d = JSON.parse(fs.readFileSync(candidate, 'utf-8'));
        if ('api_key' in d) { return candidate; }
    } catch { /* invalid JSON */ }
    return null;
}

// ---------------------------------------------------------------------------
// WebviewView provider
// ---------------------------------------------------------------------------

class CodesearchViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'tscodesearch.panel';

    private _view?: vscode.WebviewView;
    private _config: CodesearchConfig | null = null;
    private _roots: string[] = ['default'];
    private _defaultRoot = 'default';

    constructor(
        private readonly _docker: ServerManager,
        private readonly _out:   vscode.OutputChannel,
    ) {}

    private _logError(context: string, e: unknown): void {
        this._out.appendLine(`[${context}] ${e instanceof Error ? e.message : String(e)}`);
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ): void {
        this._view = webviewView;
        webviewView.webview.options = { enableScripts: true };

        const configOk = this._reloadConfig();
        webviewView.webview.html = buildWebviewHtml(getNonce(), this._roots, this._defaultRoot);

        if (!configOk) {
            vscode.commands.executeCommand('workbench.action.openSettings', 'tscodesearch.repoPath');
        }

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'openSettings') {
                vscode.commands.executeCommand('workbench.action.openSettings', 'tscodesearch.repoPath');

            } else if (msg.type === 'search') {
                if (!this._config) {
                    const ok = this._reloadConfig();
                    if (!ok) { return; }
                    // Rebuild HTML in case roots changed now that config loaded
                    webviewView.webview.html = buildWebviewHtml(getNonce(), this._roots, this._defaultRoot);
                }
                try {
                    const pr = await runSearchPipeline(
                        this._config!, msg.query as string, msg.mode as string,
                        msg.ext || '', msg.sub || '',
                        msg.root || this._defaultRoot, msg.limit || 20,
                    );
                    webviewView.webview.postMessage({
                        type: 'results',
                        hits: pr.hits,
                        found: pr.found,
                        elapsed: pr.elapsed,
                        query: msg.query,
                        mode: msg.mode,
                        ext: msg.ext || '',
                        root: msg.root || this._defaultRoot,
                        facet_counts: pr.facet_counts ?? [],
                    });
                } catch (e: unknown) {
                    this._logError('search', e);
                    webviewView.webview.postMessage({ type: 'error', message: e instanceof Error ? e.message : String(e) });
                }

            } else if (msg.type === 'expandSub') {
                this._out.appendLine(`[expandSub] received: sub=${JSON.stringify(msg.sub)} query=${JSON.stringify(msg.query)} mode=${msg.mode} ext=${JSON.stringify(msg.ext)} root=${JSON.stringify(msg.root)}`);
                if (!this._config && !this._reloadConfig()) {
                    this._out.appendLine('[expandSub] no config — aborting');
                    return;
                }
                try {
                    this._out.appendLine(`[expandSub] calling runSearchPipeline, port=${this._config!.port}`);
                    const pr = await runSearchPipeline(
                        this._config!, msg.query as string, msg.mode as string,
                        msg.ext || '', msg.sub as string,
                        msg.root || this._defaultRoot, 50,
                    );
                    this._out.appendLine(`[expandSub] result: found=${pr.found} hits=${pr.hits.length} overflow=${pr.overflow}`);
                    webviewView.webview.postMessage({
                        type: 'subResults',
                        sub: msg.sub,
                        hits: pr.hits,
                        found: pr.found,
                        elapsed: pr.elapsed,
                    });
                } catch (e: unknown) {
                    this._logError('expandSub', e);
                    webviewView.webview.postMessage({
                        type: 'subResults',
                        sub: msg.sub,
                        error: e instanceof Error ? e.message : String(e),
                    });
                }

            } else if (msg.type === 'expandFile') {
                if (!this._config && !this._reloadConfig()) { return; }
                const rootMap  = getRoots(this._config!);
                const rootPath = rootMap[msg.root as string] ?? Object.values(rootMap)[0];
                if (!rootPath) { return; }
                const fullPath = resolveFilePath(rootPath, msg.relativePath as string);
                let matches: MatchItem[] = [];
                try {
                    matches = await doQuerySingleFile(
                        this._config!, msg.mode as string, msg.query as string, fullPath,
                    );
                } catch (e: unknown) { this._logError('expandFile', e); }
                webviewView.webview.postMessage({
                    type: 'fileExpanded',
                    relativePath: msg.relativePath,
                    epoch: msg.epoch,
                    matches,
                });

            } else if (msg.type === 'openFile') {
                if (!this._config && !this._reloadConfig()) { return; }
                const rootMap = getRoots(this._config!);
                const rootPath = rootMap[msg.root as string] ?? Object.values(rootMap)[0];
                if (!rootPath) { vscode.window.showErrorMessage('TsCodeSearch: no source root configured.'); return; }
                try {
                    const fullPath = resolveFilePath(rootPath, msg.relativePath as string);
                    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(fullPath));
                    let position: vscode.Position;
                    if (typeof msg.line === 'number') {
                        const indent = doc.lineAt(msg.line).firstNonWhitespaceCharacterIndex;
                        position = new vscode.Position(msg.line, indent);
                    } else {
                        const query = (msg.query as string || '').trim().toLowerCase();
                        position = new vscode.Position(0, 0);
                        if (query) {
                            const idx = doc.getText().toLowerCase().indexOf(query);
                            if (idx >= 0) { position = doc.positionAt(idx); }
                        }
                    }
                    await vscode.window.showTextDocument(doc, {
                        preview: false,
                        selection: new vscode.Range(position, position),
                    });
                } catch (e: unknown) {
                    vscode.window.showErrorMessage(`TsCodeSearch: cannot open file — ${e instanceof Error ? e.message : e}`);
                }
            } else if (msg.type === 'jsError') {
                this._logError('webview', msg.message ?? '(unknown error)');
            }
        });
    }

    private _reloadConfig(): boolean {
        try {
            const found = findConfigPath(this._docker.repoPath);
            if (!found) {
                const configPath = this._docker.repoPath
                    ? `${this._docker.repoPath}\\config.json`
                    : 'config.json';
                const message = [
                    `${configPath} not found.`,
                    '',
                    'Run "TsCodeSearch: Set Up" to initialise the server,',
                    'or open the tscodesearch repo folder in VS Code.',
                ].join('\n');
                this._out.appendLine(`[config] Not found at: ${configPath}`);
                this._view?.webview.postMessage({ type: 'configError', message });
                return false;
            }
            this._config = loadConfig(found);
            const rootMap = getRoots(this._config);
            this._roots = Object.keys(rootMap);
            this._defaultRoot = this._roots[0] ?? 'default';
            this._out.appendLine(`[config] Loaded ${found} — roots: ${this._roots.join(', ')} | port: ${this._config.port}`);
            return true;
        } catch (e: unknown) {
            this._logError('config', e);
            this._view?.webview.postMessage({ type: 'configError', message: `Failed to load config.json — ${e instanceof Error ? e.message : String(e)}` });
            return false;
        }
    }
}

// ---------------------------------------------------------------------------
// Extension activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
    const out          = vscode.window.createOutputChannel('TsCodeSearch');
    context.subscriptions.push(out);

    const docker       = new ServerManager(context, out);
    const treeProvider = new RootsTreeProvider(docker);

    // ── Webview search panel ─────────────────────────────────────────────────
    const provider = new CodesearchViewProvider(docker, out);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(CodesearchViewProvider.viewType, provider, {
            webviewOptions: { retainContextWhenHidden: true },
        }),
    );

    // ── Roots tree view ──────────────────────────────────────────────────────
    context.subscriptions.push(
        vscode.window.registerTreeDataProvider('tscodesearch.roots', treeProvider),
    );

    // ── File watcher + status bar ────────────────────────────────────────────
    // In Docker mode use the Docker config; fall back to legacy WSL config.json.
    let watcher: FileWatcher | null = null;

    function _startWatcherAndStatus(config: CodesearchConfig): void {
        try {
            watcher = new FileWatcher(config, out);
            context.subscriptions.push(watcher);
            context.subscriptions.push(new StatusBarManager(watcher, out, treeProvider));
        } catch (e) {
            out.appendLine(`[activate] watcher/status setup error: ${e}`);
        }
    }

    // config.json always lives at <repoPath>/config.json for both modes.
    try {
        const found = findConfigPath(docker.repoPath);
        if (found) {
            docker.setDiskConfig(loadConfig(found));
            _startWatcherAndStatus(docker.getClientConfig());
            // Auto-start the indexserver in the background (start is idempotent).
            if (docker.repoPath) {
                out.appendLine('[activate] Auto-starting indexserver...');
                docker.start(line => out.appendLine(line)).catch(e => {
                    out.appendLine(`[activate] Indexserver auto-start failed: ${e}`);
                });
            }
        }
    } catch (e) {
        out.appendLine(`[activate] Config load error: ${e}`);
    }

    // ── Commands ─────────────────────────────────────────────────────────────

    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.openPanel', () => {
            void vscode.commands.executeCommand('tscodesearch.panel.focus');
        }),
    );

    // Setup wizard
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.setup', () => {
            out.show(true);
            void vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: 'TsCodeSearch Setup', cancellable: false },
                async (progress) => {
                    try {
                        await docker.setup(progress);
                        // Start watcher/status now that the server is up
                        if (!watcher) { _startWatcherAndStatus(docker.getClientConfig()); }
                        treeProvider.refresh();
                        void vscode.window.showInformationMessage('TsCodeSearch: Setup complete!');
                    } catch (e: unknown) {
                        void vscode.window.showErrorMessage(
                            `TsCodeSearch: Setup failed — ${e instanceof Error ? e.message : String(e)}`,
                        );
                    }
                },
            );
        }),
    );

    // Add a new source root
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.addRoot', async () => {
            const name = await vscode.window.showInputBox({
                prompt: 'Name for this source root',
                placeHolder: 'default',
                validateInput: (v) => v.trim() ? null : 'Name cannot be empty',
            });
            if (!name) { return; }
            const picks = await vscode.window.showOpenDialog({
                canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
                openLabel: 'Select source root folder',
            });
            if (!picks || picks.length === 0) { return; }
            docker.addRoot(name.trim(), picks[0].fsPath);
            treeProvider.refresh();
            const choice = await vscode.window.showInformationMessage(
                `TsCodeSearch: Added root "${name.trim()}". Restart the server to apply.`,
                'Restart Now',
            );
            if (choice === 'Restart Now') {
                void vscode.commands.executeCommand('tscodesearch.restartDaemon');
            }
        }),
    );

    // Remove a source root (called from tree item context menu)
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.removeRoot', async (item?: CodesearchTreeItem) => {
            const name = item?.rootName;
            if (!name) { return; }
            const confirm = await vscode.window.showWarningMessage(
                `Remove root "${name}"? The server will need to be restarted.`,
                { modal: true }, 'Remove',
            );
            if (confirm !== 'Remove') { return; }
            docker.removeRoot(name);
            treeProvider.refresh();
        }),
    );

    // Restart the server
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.restartDaemon', () => {
            out.show(true);
            void vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: 'TsCodeSearch: Restarting…', cancellable: false },
                async (progress) => {
                    try {
                        out.appendLine('[server] Restarting…');
                        await docker.restart((line) => progress.report({ message: line }));
                        treeProvider.refresh();
                        void vscode.window.showInformationMessage('TsCodeSearch: Restarted.');
                    } catch (e: unknown) {
                        void vscode.window.showErrorMessage(
                            `TsCodeSearch: Restart failed — ${e instanceof Error ? e.message : String(e)}`,
                        );
                    }
                },
            );
        }),
    );

    // Stop the server
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.stopDaemon', () => {
            void docker.stop()
                .then(() => {
                    treeProvider.refresh();
                    void vscode.window.showInformationMessage('TsCodeSearch: Stopped.');
                })
                .catch((e: unknown) => {
                    void vscode.window.showErrorMessage(
                        `TsCodeSearch: Stop failed — ${e instanceof Error ? e.message : String(e)}`,
                    );
                });
        }),
    );

    // Trigger verify/repair for a root (called from tree item context menu)
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.reindex', (item?: CodesearchTreeItem) => {
            const name = item?.rootName;
            if (!name) { return; }
            void (watcher ?? new FileWatcher(docker.getClientConfig(), out))
                .apiPost('/verify/start', { root: name, delete_orphans: true })
                .then((r) => {
                    if (r) {
                        void vscode.window.showInformationMessage(`TsCodeSearch: Re-indexing "${name}" in background.`);
                    } else {
                        void vscode.window.showWarningMessage('TsCodeSearch: Could not start re-index — is the server running?');
                    }
                });
        }),
    );

    // Refresh the roots tree view
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.refreshRoots', () => {
            treeProvider.refresh();
        }),
    );
}

export function deactivate(): void { /* nothing */ }
