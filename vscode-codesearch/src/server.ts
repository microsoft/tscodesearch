/**
 * Unified server manager for codesearch.
 *
 * Owns root management (VS Code settings), config.json generation, and server
 * lifecycle for both Docker and WSL modes.  All heavy lifting is delegated to
 * ts.mjs (Node.js CLI, no WSL required):
 *
 *   node.exe <repoPath>/ts.mjs <cmd>
 *
 * ts.mjs reads config.json directly and dispatches to Docker or WSL internals
 * based on the "mode" field.  The management API (port typesensePort+1) is the
 * same for both modes, so FileWatcher and StatusBarManager work identically.
 */

import * as vscode from 'vscode';
import * as fs    from 'fs';
import * as path  from 'path';
import * as cp    from 'child_process';
import { CodesearchConfig, getRoots as getRootsFromConfig } from './client';

export type ServerMode = 'docker' | 'wsl';

// ── Helpers ───────────────────────────────────────────────────────────────────

function cfg<T>(key: string, fallback: T): T {
    return vscode.workspace.getConfiguration('tscodesearch').get<T>(key) ?? fallback;
}

/**
 * Scan VS Code workspace folders for one that contains a valid config.json
 * (identified by having an "api_key" field).  Returns the folder path (the
 * repo root) or null if nothing is found.
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

    /**
     * In WSL mode, config.json on disk is the source of truth.
     * Call this once at startup after discovering the config file.
     */
    setDiskConfig(config: CodesearchConfig): void {
        this._diskConfig = config;
    }

    // ── Mode + settings ──────────────────────────────────────────────────────

    get mode():          ServerMode { return (this._diskConfig?.mode as ServerMode | undefined) ?? 'docker'; }
    get mcpPort():       number     { return cfg('mcpPort',         3000); }
    get typesensePort(): number     { return cfg('typesensePort',   8108); }
    get apiPort():       number     { return this.typesensePort + 1; }
    /** Explicit setting takes precedence; falls back to workspace folder auto-detection. */
    get repoPath(): string { return cfg('repoPath', '') || discoverRepoPath() || ''; }
    // Docker-specific
    get containerName(): string { return cfg('dockerContainer', 'codesearch'); }
    get imageName():     string { return cfg('dockerImage',     'codesearch-mcp'); }
    get dataVolume():    string { return `${this.containerName}_data`; }

    /** Human-readable display name for the Roots tree view. */
    get displayName(): string {
        return this.mode === 'docker' ? this.containerName : 'Indexserver (WSL)';
    }

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

    // ── Config generation ────────────────────────────────────────────────────

    /**
     * Sync VS Code settings (port, docker_container, docker_image) into config.json
     * so that ts.mjs picks them up on the next invocation.  Returns the config path.
     */
    writeConfig(): string {
        const config = this._readConfigFile();
        config.port             = this.typesensePort;
        config.docker_container = this.containerName;
        config.docker_image     = this.imageName;
        this._writeConfigFile(config);
        return this._configFilePath();
    }

    /** Config for the VS Code extension (Windows paths for file resolution). */
    getClientConfig(): CodesearchConfig {
        if (this._diskConfig) { return this._diskConfig; }
        return this._readConfigFile();
    }

    // ── CLI invocation ───────────────────────────────────────────────────────

    private async _run(cmd: string, onLine?: (line: string) => void): Promise<void> {
        if (!this.repoPath) {
            throw new Error('tscodesearch repo not found. Open the repo folder in VS Code, or set tscodesearch.repoPath explicitly.');
        }
        // Sync VS Code settings into config.json before every lifecycle command
        // so ts.mjs reads up-to-date port / container / image values.
        this.writeConfig();

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
     * Full setup wizard.  Prompts for repo path if not configured, then
     * calls `ts.mjs setup` which builds the Docker image (if needed) and starts
     * the container.
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
        await this._run('setup', (line) => {
            progress.report({ message: line });
        });
    }
}
