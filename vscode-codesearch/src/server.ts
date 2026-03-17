/**
 * Unified server manager for codesearch.
 *
 * Owns root management (VS Code settings), config.json generation, and server
 * lifecycle for both Docker and WSL modes.  Heavy lifting is delegated to two
 * shell scripts that are called via `wsl.exe bash -l`:
 *
 *   tscodesearch-docker.sh  — builds and runs the Docker container
 *   tscodesearch-wsl.sh     — manages the WSL indexserver (Python venvs + ts.sh)
 *
 * Both modes expose the same management API (port typesensePort+1) so
 * FileWatcher and StatusBarManager work identically for both.
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

function storedApiKey(context: vscode.ExtensionContext): string {
    let key = context.globalState.get<string>('server.apiKey');
    if (!key) {
        const bytes = new Uint8Array(20);
        for (let i = 0; i < 20; i++) { bytes[i] = Math.floor(Math.random() * 256); }
        key = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
        void context.globalState.update('server.apiKey', key);
    }
    return key;
}

/** Convert a Windows path to its WSL /mnt/<drive>/... equivalent. */
function winToWsl(winPath: string): string {
    return winPath
        .replace(/\\/g, '/')
        .replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`);
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
            else { reject(new Error(`${args[args.length - 1]?.split('/').pop() ?? cmd} exited with code ${code}`)); }
        });
        proc.on('error', (err) => {
            // wsl.exe not found → helpful message
            if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
                reject(new Error('WSL is not available. Install WSL 2 from the Microsoft Store.'));
            } else {
                reject(err);
            }
        });
    });
}

// ── ServerManager ─────────────────────────────────────────────────────────────

export class ServerManager {
    private readonly _context:    vscode.ExtensionContext;
    private readonly _storageDir: string;
    private readonly _out:        vscode.OutputChannel;
    private _diskConfig:          CodesearchConfig | null = null;

    constructor(context: vscode.ExtensionContext, out: vscode.OutputChannel) {
        this._context    = context;
        this._storageDir = context.globalStorageUri.fsPath;
        this._out        = out;
        fs.mkdirSync(this._storageDir, { recursive: true });
    }

    /**
     * In WSL mode, config.json on disk is the source of truth.
     * Call this once at startup after discovering the config file.
     * Roots, port, and API key will all come from the file instead of VS Code settings.
     */
    setDiskConfig(config: CodesearchConfig): void {
        this._diskConfig = config;
    }

    // ── Mode + settings ──────────────────────────────────────────────────────

    get mode():          ServerMode { return cfg<ServerMode>('mode', 'docker'); }
    get mcpPort():       number     { return cfg('mcpPort',         3000); }
    get typesensePort(): number     { return cfg('typesensePort',   8108); }
    get apiPort():       number     { return this.typesensePort + 1; }
    get repoPath():      string     { return cfg('repoPath',        ''); }
    get apiKey():        string     { return storedApiKey(this._context); }

    // Docker-specific (ignored in WSL mode)
    get containerName(): string { return cfg('dockerContainer', 'codesearch'); }
    get imageName():     string { return cfg('dockerImage',     'codesearch-mcp'); }
    get dataVolume():    string { return `${this.containerName}_data`; }

    /** Human-readable display name for the Roots tree view. */
    get displayName(): string {
        return this.mode === 'docker' ? this.containerName : 'Indexserver (WSL)';
    }

    // ── Root management ──────────────────────────────────────────────────────

    getRoots(): Record<string, string> {
        if (this.mode === 'wsl' && this._diskConfig) {
            return getRootsFromConfig(this._diskConfig);
        }
        return cfg<Record<string, string>>('roots', {});
    }

    async addRoot(name: string, winPath: string): Promise<void> {
        const roots = { ...this.getRoots(), [name]: winPath };
        await vscode.workspace.getConfiguration('tscodesearch').update(
            'roots', roots, vscode.ConfigurationTarget.Global,
        );
        this.writeConfig();
    }

    async removeRoot(name: string): Promise<void> {
        const roots = { ...this.getRoots() };
        delete roots[name];
        await vscode.workspace.getConfiguration('tscodesearch').update(
            'roots', roots, vscode.ConfigurationTarget.Global,
        );
        this.writeConfig();
    }

    // ── Config generation ────────────────────────────────────────────────────

    /**
     * Write config.json and return its Windows path.
     *
     * WSL mode:    writes directly to <repoPath>/config.json (Windows paths).
     *              This is the single file read by both the MCP server and the
     *              indexserver, so writing here keeps everything in sync without
     *              a separate sync_config step.
     * Docker mode: writes to extension storage (container paths: /source/<name>),
     *              then the shell script mounts it into the container.
     */
    writeConfig(): string {
        const roots = this.getRoots();

        if (this.mode === 'wsl' && this.repoPath) {
            // WSL mode: write Windows paths directly to the shared config.json.
            const config = {
                api_key: this.apiKey,
                port:    this.typesensePort,
                roots,
            };
            const dest = path.join(this.repoPath, 'config.json');
            fs.writeFileSync(dest, JSON.stringify(config, null, 2), 'utf-8');
            return dest;
        }

        // Docker mode: translate to container paths.
        const configRoots:  Record<string, string> = {};
        const hostMounts:   Record<string, string> = {};
        for (const [name, winPath] of Object.entries(roots)) {
            configRoots[name] = `/source/${name}`;
            hostMounts[name]  = winToWsl(winPath);  // WSL path for docker -v mounts
        }
        const config = {
            api_key:  this.apiKey,
            port:     this.typesensePort,
            roots:    configRoots,
            _mounts:  hostMounts,  // host-side paths read by tscodesearch-docker.sh
        };
        const dest = path.join(this._storageDir, 'config.json');
        fs.writeFileSync(dest, JSON.stringify(config, null, 2), 'utf-8');
        return dest;
    }

    /** Config for the VS Code extension (Windows paths for file resolution). */
    getClientConfig(): CodesearchConfig {
        if (this.mode === 'wsl' && this._diskConfig) {
            return this._diskConfig;
        }
        return {
            api_key: this.apiKey,
            port:    this.typesensePort,
            roots:   this.getRoots(),
        };
    }

    // ── Script invocation ────────────────────────────────────────────────────

    private _scriptWslPath(): string {
        const repoPath = this.repoPath;
        if (!repoPath) { throw new Error('tscodesearch.repoPath is not set. Point it to the tscodesearch repo root.'); }
        const name = this.mode === 'docker' ? 'tscodesearch-docker.sh' : 'tscodesearch-wsl.sh';
        return winToWsl(path.join(repoPath, name));
    }

    /** Build the option flags passed to the shell script after the command name. */
    private _buildFlags(): string[] {
        const configWin = this.writeConfig();
        const flags = [
            '--config',   winToWsl(configWin),
            '--mcp-port', String(this.mcpPort),
        ];
        if (this.repoPath) {
            flags.push('--repo-path', winToWsl(this.repoPath));
        }
        if (this.mode === 'docker') {
            flags.push(
                '--container', this.containerName,
                '--image',     this.imageName,
                '--data-vol',  this.dataVolume,
            );
        }
        return flags;
    }

    private async _run(cmd: string, onLine?: (line: string) => void): Promise<void> {
        if (!this.repoPath) {
            throw new Error('tscodesearch.repoPath is not set. Point it to the tscodesearch repo root.');
        }
        const log = (l: string) => { this._out.appendLine(l); onLine?.(l); };
        await spawnLines(
            'wsl.exe',
            ['bash', '-l', this._scriptWslPath(), cmd, ...this._buildFlags()],
            log,
        );
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

    async registerMcp(): Promise<void> {
        await this._run('register-mcp');
    }

    /**
     * Full setup wizard.  Prompts for repo path if not configured, then
     * calls the mode-specific setup script (installs deps, starts service,
     * registers MCP).
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
