/**
 * Windows filesystem watcher for codesearch.
 *
 * Uses VS Code's native file system watcher (backed by ReadDirectoryChangesW on
 * Windows) to detect changes under Windows-path source roots, then forwards
 * batched file-change events to the indexserver management API (POST /file-events).
 *
 * On startup the watcher:
 *   1. Pauses the WSL PollingObserver so changes are not double-processed.
 *   2. Triggers a background verify pass to catch up on any changes that
 *      accumulated while VS Code was closed.
 *   3. Begins watching for new changes and flushing them in 2-second batches.
 *
 * On disposal (VS Code closed / extension deactivated) the watcher resumes the
 * WSL PollingObserver so the indexserver continues to receive updates.
 *
 * Only roots with Windows-style drive paths (e.g. C:/) are watched here.
 * Native Linux / WSL paths are left to the indexserver's PollingObserver.
 */

import * as vscode from 'vscode';
import * as http from 'http';
import { CodesearchConfig, getRoots } from './client';

const WIN_PATH_RE = /^[A-Za-z]:[/\\]/;

// Mirror EXCLUDE_DIRS from indexserver/config.py — skip events from these dirs
const EXCLUDED_PATH_RE = /(^|[/\\])(\.git|obj|bin|node_modules|\.venv|__pycache__|\.vs|Target|Build|Import|nugetcache|target|debug|ship|x64|x86)([/\\]|$)/i;

const DEBOUNCE_MS = 2000;

export class FileWatcher {
    private _fsWatchers: vscode.FileSystemWatcher[] = [];
    private _disposed   = false;
    private _pending    = new Map<string, 'upsert' | 'delete'>();
    private _flushTimer: ReturnType<typeof setTimeout> | null = null;
    private _inFlight   = false;

    readonly apiPort: number;
    readonly apiKey:  string;
    private readonly _roots: Array<[string, string]>; // [rootName, winPath]

    constructor(config: CodesearchConfig) {
        this.apiPort = (config.port ?? 8108) + 1;
        this.apiKey  = config.api_key;
        const allRoots = getRoots(config);
        this._roots = Object.entries(allRoots).filter(([, p]) => WIN_PATH_RE.test(p));
        if (this._roots.length > 0) {
            void this._start();
        }
    }

    /** True when this watcher owns file-change delivery (VS Code native watchers are active). */
    get isActive(): boolean { return this._roots.length > 0 && !this._disposed; }

    // ── Startup ────────────────────────────────────────────────────────────────

    private async _start(): Promise<void> {
        // Pause the WSL PollingObserver — we handle events natively from here.
        await this.apiPost('/watcher/pause');

        // Trigger a background verify for each root to catch up on changes that
        // occurred while VS Code was closed (git pulls, builds, branch switches, etc.).
        for (const [name] of this._roots) {
            await this.apiPost('/verify/start', { root: name, delete_orphans: true });
        }

        if (this._disposed) { return; }

        for (const [, root] of this._roots) {
            if (this._disposed) { break; }
            const pattern = new vscode.RelativePattern(vscode.Uri.file(root), '**/*');
            const watcher = vscode.workspace.createFileSystemWatcher(pattern);
            watcher.onDidCreate(uri => this._queue(uri.fsPath, 'upsert'));
            watcher.onDidChange(uri => this._queue(uri.fsPath, 'upsert'));
            watcher.onDidDelete(uri => this._queue(uri.fsPath, 'delete'));
            this._fsWatchers.push(watcher);
        }
    }

    // ── Event queue + debounced flush ──────────────────────────────────────────

    private _queue(rawPath: string, action: 'upsert' | 'delete'): void {
        if (this._disposed) { return; }
        if (EXCLUDED_PATH_RE.test(rawPath)) { return; }
        this._pending.set(rawPath.replace(/\\/g, '/'), action);
        this._scheduleFlush();
    }

    private _scheduleFlush(): void {
        if (this._inFlight) { return; } // completion handler will drain
        if (this._flushTimer) { clearTimeout(this._flushTimer); }
        this._flushTimer = setTimeout(() => { void this._flush(); }, DEBOUNCE_MS);
    }

    private async _flush(): Promise<void> {
        if (this._inFlight || this._pending.size === 0 || this._disposed) { return; }
        this._inFlight  = true;
        this._flushTimer = null;

        const events = Array.from(this._pending.entries()).map(([path, action]) => ({ path, action }));
        this._pending.clear();

        try {
            const result = await this.apiPost('/file-events', { events });
            console.log(`[tscodesearch watcher] sent ${events.length} event(s): queued=${result?.['queued'] ?? '?'} deduped=${result?.['deduped'] ?? '?'}`);
        } catch (e) {
            console.error(`[tscodesearch watcher] flush error: ${e}`);
            // Re-queue so nothing is lost on a transient network error
            for (const ev of events) { this._pending.set(ev.path, ev.action); }
        }

        this._inFlight = false;
        if (this._pending.size > 0) { await this._flush(); }
    }

    // ── HTTP helper (shared with status.ts via the public overload) ────────────

    apiPost(path: string, body?: unknown): Promise<Record<string, unknown> | null> {
        return new Promise((resolve) => {
            const bodyStr = body ? JSON.stringify(body) : '';
            const req = http.request(
                {
                    hostname: 'localhost',
                    port:     this.apiPort,
                    path,
                    method:   'POST',
                    headers:  {
                        'X-TYPESENSE-API-KEY': this.apiKey,
                        ...(body ? {
                            'Content-Type':   'application/json',
                            'Content-Length': Buffer.byteLength(bodyStr),
                        } : {}),
                    },
                },
                (res) => {
                    let data = '';
                    res.on('data', (chunk: Buffer) => { data += chunk; });
                    res.on('end', () => {
                        try { resolve(JSON.parse(data) as Record<string, unknown>); }
                        catch { resolve(null); }
                    });
                },
            );
            req.setTimeout(5000, () => { req.destroy(); resolve(null); });
            req.on('error', () => resolve(null));
            if (body) { req.write(bodyStr); }
            req.end();
        });
    }

    apiGet(path: string): Promise<Record<string, unknown> | null> {
        return new Promise((resolve) => {
            const req = http.request(
                {
                    hostname: 'localhost',
                    port:     this.apiPort,
                    path,
                    method:   'GET',
                    headers:  { 'X-TYPESENSE-API-KEY': this.apiKey },
                },
                (res) => {
                    let data = '';
                    res.on('data', (chunk: Buffer) => { data += chunk; });
                    res.on('end', () => {
                        try { resolve(JSON.parse(data) as Record<string, unknown>); }
                        catch { resolve(null); }
                    });
                },
            );
            req.setTimeout(5000, () => { req.destroy(); resolve(null); });
            req.on('error', () => resolve(null));
            req.end();
        });
    }

    // ── Disposal ───────────────────────────────────────────────────────────────

    dispose(): void {
        this._disposed = true;
        if (this._flushTimer) { clearTimeout(this._flushTimer); }
        for (const w of this._fsWatchers) { w.dispose(); }
        this._fsWatchers = [];
        // Resume the WSL PollingObserver — it handles changes while VS Code is closed
        void this.apiPost('/watcher/resume');
    }
}
