/**
 * Daemon manager for codesearch.
 *
 * Owns root management (config.json) and daemon lifecycle. All heavy lifting
 * is delegated to ts.mjs:
 *
 *   node.exe <repoPath>/ts.mjs <cmd>
 *
 * The daemon (tsquery_server.py) listens on config.json's `port` and is
 * a single in-process Tantivy-backed indexer — there is no Docker or WSL
 * lifecycle to manage anymore.
 */

import * as vscode from 'vscode';
import * as fs    from 'fs';
import * as path  from 'path';
import * as cp    from 'child_process';
import { CodesearchConfig, getRoots as getRootsFromConfig } from './client';

// ── Helpers ───────────────────────────────────────────────────────────────────

function cfg<T>(key: string, fallback: T): T {
    return vscode.workspace.getConfiguration('tscodesearch').get<T>(key) ?? fallback;
}

/**
 * Scan VS Code workspace folders for one that contains a valid config.json
 * (identified by having an "api_key" field).
 */
function discoverRepoPath(): string | null {
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
        const candidate = path.join(folder.uri.fsPath, 'config.json');
        if (!fs.existsSync(candidate)) { continue; }
        try {
            const d = JSON.parse(fs.readFileSync(candidate, 'utf-8'));
            if ('api_key' in d) { return folder.uri.fsPath; }
        } catch { /* skip */ }
    }
    return null;
}

function spawnLines(
    cmd: string,
    args: string[],
    onLine: (line: string) => void,
): Promise<void> {
    return new Promise((resolve, reject) => {
        const proc = cp.spawn(cmd, args, { windowsHide: true });
        const onData = (data: Buffer) => {
            for (const line of data.toString().split(/\r?\n/)) {
                if (line.trim()) { onLine(line); }
            }
        };
        proc.stdout.on('data', onData);
        proc.stderr.on('data', onData);
        proc.on('close', (code) => {
            if (code === 0) { resolve(); }
            else { reject(new Error(`ts ${args[0] ?? ''} exited with code ${code}`)); }
        });
        proc.on('error', reject);
    });
}

// ── ServerManager ─────────────────────────────────────────────────────────────

export class ServerManager {
    private readonly _out:  vscode.OutputChannel;
    private _diskConfig:    CodesearchConfig | null = null;

    constructor(_context: vscode.ExtensionContext, out: vscode.OutputChannel) {
        this._out = out;
    }

    /** Once the extension finds the on-disk config it should share it here. */
    setDiskConfig(config: CodesearchConfig): void {
        this._diskConfig = config;
    }

    // ── Settings ─────────────────────────────────────────────────────────────

    get mcpPort(): number { return cfg('mcpPort', 3000); }
    get port():    number { return this._diskConfig?.port ?? cfg('port', 8108); }
    /** Back-compat alias used by the tree view. */
    get apiPort(): number { return this.port; }
    /** Explicit setting takes precedence; falls back to workspace folder auto-detection. */
    get repoPath(): string { return cfg('repoPath', '') || discoverRepoPath() || ''; }

    /** Human-readable display name for the Roots tree view. */
    get displayName(): string { return 'Codesearch daemon'; }

    // ── Config file helpers ──────────────────────────────────────────────────

    private _configFilePath(): string {
        if (!this.repoPath) {
            throw new Error('tscodesearch repo not found. Open the repo folder in VS Code, or set tscodesearch.repoPath explicitly.');
        }
        return path.join(this.repoPath, 'config.json');
    }

    private _readConfigFile(): CodesearchConfig {
        return JSON.parse(fs.readFileSync(this._configFilePath(), 'utf-8')) as CodesearchConfig;
    }

    private _writeConfigFile(config: CodesearchConfig): void {
        fs.writeFileSync(this._configFilePath(), JSON.stringify(config, null, 2), 'utf-8');
        this._diskConfig = config;
    }

    // ── Root management ──────────────────────────────────────────────────────

    getRoots(): Record<string, string> {
        if (this._diskConfig) { return getRootsFromConfig(this._diskConfig); }
        if (this.repoPath) {
            try { return getRootsFromConfig(this._readConfigFile()); } catch { /* fall through */ }
        }
        return {};
    }

    addRoot(name: string, winPath: string): void {
        const config = this._readConfigFile();
        config.roots = { ...(config.roots ?? {}), [name]: { path: winPath } };
        this._writeConfigFile(config);
    }

    removeRoot(name: string): void {
        const config = this._readConfigFile();
        const { [name]: _, ...rest } = (config.roots ?? {});
        config.roots = rest;
        this._writeConfigFile(config);
    }

    /** Config for the VS Code extension. */
    getClientConfig(): CodesearchConfig {
        if (this._diskConfig) { return this._diskConfig; }
        return this._readConfigFile();
    }

    // ── CLI invocation ───────────────────────────────────────────────────────

    private async _run(cmd: string, onLine?: (line: string) => void): Promise<void> {
        if (!this.repoPath) {
            throw new Error('tscodesearch repo not found. Open the repo folder in VS Code, or set tscodesearch.repoPath explicitly.');
        }
        const tsMjs = path.join(this.repoPath, 'ts.mjs');
        const log   = (l: string) => { this._out.appendLine(l); onLine?.(l); };
        await spawnLines('node.exe', [tsMjs, cmd], log);
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────

    async start(onLine?: (line: string) => void): Promise<void> {
        await this._run('start', onLine);
    }

    async stop(): Promise<void> {
        await this._run('stop');
    }

    async restart(onLine?: (line: string) => void): Promise<void> {
        await this._run('restart', onLine);
    }

    /**
     * Setup wizard: prompts for the repo path if not configured, then starts
     * the daemon.
     */
    async setup(progress: vscode.Progress<{ message: string }>): Promise<void> {
        if (!this.repoPath) {
            const pick = await vscode.window.showOpenDialog({
                canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
                openLabel: 'Select tscodesearch repo root',
            });
            if (!pick || pick.length === 0) { throw new Error('Setup cancelled.'); }
            await vscode.workspace.getConfiguration('tscodesearch').update(
                'repoPath', pick[0].fsPath, vscode.ConfigurationTarget.Global,
            );
        }
        await this._run('start', (line) => {
            progress.report({ message: line });
        });
    }
}
