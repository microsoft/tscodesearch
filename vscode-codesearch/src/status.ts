/**
 * Status bar item for TsCodeSearch.
 *
 * Polls the indexserver management API (/status, /verify/status) every 5 seconds
 * and updates a VS Code status bar item with the current state.
 *
 * Display:
 *   $(search) 42k docs          — healthy, index in sync
 *   $(sync~spin) indexing…      — verifier running or watcher queue non-empty
 *   $(warning) offline          — indexserver unreachable
 *
 * Hover tooltip shows full detail: doc counts per root, watcher state,
 * queue depth, and verifier progress if a scan is in progress.
 */

import * as vscode from 'vscode';
import { FileWatcher } from './watcher';
import { RootsTreeProvider, StatusDetail } from './treeview';

interface StatusResponse {
    watcher?:   { state?: string; queue_depth?: number; paused?: boolean };
    heartbeat?: { state?: string };
    verifier?:  { state?: string };
    collections?: Record<string, {
        collection?:        string;
        num_documents?:     number;
        collection_exists?: boolean;
        schema_ok?:         boolean;
    }>;
}

interface VerifyStatusResponse {
    running?:    boolean;
    fs_files?:   number;
    indexed?:    number;
    missing?:    number;
    stale?:      number;
    orphaned?:   number;
    duration_s?: number;
    error?:      string;
}

function fmtDocs(n: number): string {
    if (n >= 1_000_000) { return `${(n / 1_000_000).toFixed(1)}M`; }
    if (n >= 1_000)     { return `${Math.round(n / 1_000)}k`; }
    return String(n);
}

/**
 * Pure helper — maps a raw /status response + VS Code watcher state to the
 * StatusDetail that feeds the tree view.  Exported for unit testing.
 */
export function buildStatusDetail(
    statusWatcher:    StatusResponse['watcher'],
    statusVerifier:   StatusResponse['verifier'],
    verifyStatus:     VerifyStatusResponse | null,
    vsCodeWatcherActive: boolean,
): import('./treeview').StatusDetail {
    const rawWatcherState = statusWatcher?.state ?? 'unknown';
    const watcherState = rawWatcherState === 'paused' && vsCodeWatcherActive
        ? 'paused (windows fs watcher active)'
        : rawWatcherState;
    const queueDepth      = statusWatcher?.queue_depth ?? 0;
    const verifierRunning = verifyStatus?.running === true || statusVerifier?.state === 'running';
    return {
        watcherState,
        queueDepth,
        verifierRunning,
        verifierChecked: verifyStatus?.indexed ?? verifyStatus?.fs_files ?? 0,
        verifierTotal:   verifyStatus?.fs_files ?? 0,
        verifierMissing: verifyStatus?.missing  ?? 0,
        verifierStale:   verifyStatus?.stale    ?? 0,
    };
}

export class StatusBarManager {
    private readonly _item:         vscode.StatusBarItem;
    private readonly _watcher:      FileWatcher;
    private readonly _treeProvider: RootsTreeProvider | null;
    private readonly _log:          vscode.OutputChannel;
    private _timer: ReturnType<typeof setInterval> | null = null;
    private _disposed = false;

    constructor(watcher: FileWatcher, treeProvider: RootsTreeProvider | null = null) {
        this._watcher      = watcher;
        this._treeProvider = treeProvider;
        this._log = vscode.window.createOutputChannel('TsCodeSearch');
        this._item = vscode.window.createStatusBarItem(
            'tscodesearch.status',
            vscode.StatusBarAlignment.Right,
            100,
        );
        this._item.name    = 'TsCodeSearch';
        this._item.text    = '$(search) TsCodeSearch';
        this._item.tooltip = 'TsCodeSearch — connecting…';
        this._item.show();

        void this._poll();
        this._timer = setInterval(() => { void this._poll(); }, 5000);
    }

    private async _poll(): Promise<void> {
        if (this._disposed) { return; }
        try {
            await this._doPoll();
        } catch (e) {
            this._log.appendLine(`[${new Date().toISOString()}] poll error: ${e}`);
            console.error('[tscodesearch status] poll error:', e);
        }
    }

    private async _doPoll(): Promise<void> {
        if (this._disposed) { return; }

        const [status, verifyStatus] = await Promise.all([
            this._watcher.apiGet('/status') as Promise<StatusResponse | null>,
            this._watcher.apiGet('/verify/status') as Promise<VerifyStatusResponse | null>,
        ]);

        if (this._disposed) { return; }

        const statusError = (status as Record<string, unknown> | null)?.['error'] as string | undefined;
        if (statusError) {
            this._log.appendLine(`[${new Date().toISOString()}] server error: ${statusError} (wrong API key?)`);
        }

        if (!status || statusError) {
            const isAuth = statusError?.toLowerCase().includes('unauthorized');
            const label  = isAuth ? 'wrong API key' : statusError ? `error: ${statusError}` : 'offline';
            this._item.text    = `$(warning) TsCodeSearch: ${label}`;
            this._item.tooltip = new vscode.MarkdownString(
                isAuth
                    ? '**TsCodeSearch** — API key mismatch\n\nThe extension API key does not match the running server.\n\nCheck `tscodesearch.configPath` or `tscodesearch.roots` settings.'
                    : '**TsCodeSearch** — server unreachable\n\nUse the Roots panel to set up or restart the server.',
            );
            this._treeProvider?.updateFromStatus(false, {}, {
                watcherState: label, queueDepth: 0,
                verifierRunning: false, verifierChecked: 0, verifierTotal: 0,
                verifierMissing: 0, verifierStale: 0,
            });
            return;
        }

        // ── Re-pause WSL watcher if VS Code watcher is active but server restarted ──
        if (this._watcher.isActive && status.watcher?.paused === false) {
            void this._watcher.apiPost('/watcher/pause');
        }

        // ── Compute aggregate doc count ──────────────────────────────────────
        const collections = status.collections ?? {};
        const totalDocs = Object.values(collections)
            .reduce((sum, c) => sum + (c.num_documents ?? 0), 0);

        // ── Determine overall state ──────────────────────────────────────────
        const verifierRunning = verifyStatus?.running === true
            || status.verifier?.state === 'running';
        const queueDepth = status.watcher?.queue_depth ?? 0;
        const isIndexing  = verifierRunning || queueDepth > 0;

        // ── Feed tree view ───────────────────────────────────────────────────
        if (this._treeProvider) {
            const docCounts: Record<string, number> = {};
            for (const [name, col] of Object.entries(collections)) {
                docCounts[name] = col.num_documents ?? 0;
            }
            const detail = buildStatusDetail(
                status.watcher, status.verifier, verifyStatus, this._watcher.isActive,
            );
            this._treeProvider.updateFromStatus(true, docCounts, detail);
        }

        // ── Status bar text ──────────────────────────────────────────────────
        if (isIndexing) {
            this._item.text = `$(sync~spin) TsCodeSearch: indexing\u2026`;
        } else {
            this._item.text = totalDocs > 0
                ? `$(search) TsCodeSearch: ${fmtDocs(totalDocs)} docs`
                : `$(search) TsCodeSearch`;
        }

        // ── Tooltip ──────────────────────────────────────────────────────────
        const lines: string[] = ['**TsCodeSearch**', ''];

        // Per-root doc counts
        const rootEntries = Object.entries(collections);
        if (rootEntries.length === 1) {
            const [, col] = rootEntries[0];
            lines.push(`**${fmtDocs(col.num_documents ?? 0)}** documents indexed`);
        } else {
            for (const [name, col] of rootEntries) {
                lines.push(`- **${name}**: ${fmtDocs(col.num_documents ?? 0)} docs`);
            }
        }

        lines.push('');

        // Watcher state
        const watcherState = status.watcher?.state ?? 'unknown';
        const queueStr = queueDepth > 0 ? ` (${queueDepth} pending)` : '';
        lines.push(`Watcher: ${watcherState}${queueStr}`);

        // Verifier state
        if (verifierRunning && verifyStatus) {
            const checked = verifyStatus.indexed ?? verifyStatus.fs_files ?? 0;
            const total   = verifyStatus.fs_files ?? 0;
            const pct     = total > 0 ? ` (${Math.round((checked / total) * 100)}%)` : '';
            lines.push(`Verifier: running — ${fmtDocs(checked)} / ${fmtDocs(total)} files${pct}`);
            if ((verifyStatus.missing ?? 0) + (verifyStatus.stale ?? 0) > 0) {
                lines.push(`  Missing: ${verifyStatus.missing ?? 0}  Stale: ${verifyStatus.stale ?? 0}`);
            }
        } else {
            lines.push(`Verifier: ${status.verifier?.state ?? 'idle'}`);
        }

        const md = new vscode.MarkdownString(lines.join('\n'));
        md.isTrusted = false;
        this._item.tooltip = md;
    }

    dispose(): void {
        this._disposed = true;
        if (this._timer !== null) { clearInterval(this._timer); }
        this._item.dispose();
        this._log.dispose();
    }
}
