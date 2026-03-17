/**
 * Tree view for the "Roots" panel in the Code Search activity bar.
 *
 * Shows:
 *   ● Server node — running / stopped / unknown
 *     ├── Root "default"  C:/proj/src  (42k docs)
 *     ├── Root "other"    D:/other/src (12k docs)
 *     └── [Add root…]
 *
 * Inline actions on root nodes:
 *   - Remove (trash icon)
 *   - Re-index (sync icon)
 */

import * as vscode from 'vscode';
import { ServerManager } from './server';

// ---------------------------------------------------------------------------
// Tree item types
// ---------------------------------------------------------------------------

type NodeKind = 'container' | 'status' | 'root' | 'add';

export class CodesearchTreeItem extends vscode.TreeItem {
    constructor(
        public readonly kind: NodeKind,
        label: string,
        collapsible: vscode.TreeItemCollapsibleState,
        public readonly rootName?: string,
    ) {
        super(label, collapsible);
        this.contextValue = kind;
    }
}

export interface StatusDetail {
    watcherState:    string;
    queueDepth:      number;
    verifierRunning: boolean;
    verifierChecked: number;
    verifierTotal:   number;
    verifierMissing: number;
    verifierStale:   number;
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export class RootsTreeProvider implements vscode.TreeDataProvider<CodesearchTreeItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<CodesearchTreeItem | undefined | void>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    private _docCounts:    Record<string, number> = {};
    private _serverRunning: boolean | null = null;
    private _detail: StatusDetail = {
        watcherState: 'unknown', queueDepth: 0,
        verifierRunning: false, verifierChecked: 0, verifierTotal: 0,
        verifierMissing: 0, verifierStale: 0,
    };

    constructor(private readonly _server: ServerManager) {}

    refresh(): void {
        this._onDidChangeTreeData.fire();
    }

    /** Called by StatusBarManager after each poll so the tree stays in sync. */
    updateFromStatus(
        serverRunning: boolean,
        docCounts: Record<string, number>,
        detail: StatusDetail,
    ): void {
        this._serverRunning = serverRunning;
        this._docCounts     = docCounts;
        this._detail        = detail;
        this._onDidChangeTreeData.fire();
    }

    getTreeItem(item: CodesearchTreeItem): vscode.TreeItem {
        return item;
    }

    getChildren(parent?: CodesearchTreeItem): CodesearchTreeItem[] {
        if (!parent) {
            return [this._buildServerNode()];
        }

        if (parent.kind === 'container') {
            const items: CodesearchTreeItem[] = [];

            if (this._serverRunning) {
                items.push(...this._buildStatusNodes());
            }

            const roots = this._server.getRoots();
            for (const [name, winPath] of Object.entries(roots)) {
                items.push(this._buildRootNode(name, winPath));
            }

            const add = new CodesearchTreeItem('add', 'Add root…', vscode.TreeItemCollapsibleState.None);
            add.command  = { command: 'tscodesearch.addRoot', title: 'Add root' };
            add.iconPath = new vscode.ThemeIcon('add');
            items.push(add);

            return items;
        }

        return [];
    }

    // ── Private builders ─────────────────────────────────────────────────────

    private _buildServerNode(): CodesearchTreeItem {
        const running  = this._serverRunning;
        const name     = this._server.displayName;
        const nodeKind = this._server.mode === 'docker' ? 'Container' : 'Indexserver';
        let icon: string;
        let desc: string;

        if (running === true) {
            const rootCount = Object.keys(this._server.getRoots()).length;
            icon = this._detail.verifierRunning || this._detail.queueDepth > 0 ? 'sync~spin' : 'vm-running';
            desc = `running — ${rootCount} root${rootCount === 1 ? '' : 's'}`;
        } else if (running === false) {
            icon = 'vm-outline';
            desc = 'stopped';
        } else {
            icon = 'question';
            desc = 'checking…';
        }

        const node = new CodesearchTreeItem(
            'container', name, vscode.TreeItemCollapsibleState.Expanded,
        );
        node.description = desc;
        node.iconPath    = new vscode.ThemeIcon(icon);

        if (running) {
            const portInfo = `MCP :${this._server.mcpPort}  Typesense :${this._server.typesensePort}  API :${this._server.apiPort}`;
            node.tooltip = `${nodeKind}: ${name}\nPorts: ${portInfo}`;
        } else {
            node.tooltip = `${name} is not running.\nRun "TsCodeSearch: Set Up" or "TsCodeSearch: Restart".`;
        }

        return node;
    }

    private _buildStatusNodes(): CodesearchTreeItem[] {
        const items: CodesearchTreeItem[] = [];
        const d = this._detail;

        // Watcher
        const watcherNode = new CodesearchTreeItem('status', 'Watcher', vscode.TreeItemCollapsibleState.None);
        if (d.queueDepth > 0) {
            watcherNode.description = `${d.watcherState} — ${d.queueDepth} queued`;
            watcherNode.iconPath    = new vscode.ThemeIcon('sync~spin');
        } else {
            watcherNode.description = d.watcherState;
            watcherNode.iconPath    = new vscode.ThemeIcon('eye');
        }
        items.push(watcherNode);

        // Verifier
        const verNode = new CodesearchTreeItem('status', 'Verifier', vscode.TreeItemCollapsibleState.None);
        if (d.verifierRunning) {
            const pct = d.verifierTotal > 0
                ? ` ${Math.round((d.verifierChecked / d.verifierTotal) * 100)}%`
                : '';
            const detail = d.verifierTotal > 0
                ? ` (${fmtDocs(d.verifierChecked)} / ${fmtDocs(d.verifierTotal)} files)`
                : '';
            verNode.description = `indexing${pct}${detail}`;
            verNode.iconPath    = new vscode.ThemeIcon('sync~spin');
            if (d.verifierMissing + d.verifierStale > 0) {
                verNode.tooltip = `Missing: ${d.verifierMissing}  Stale: ${d.verifierStale}`;
            }
        } else {
            verNode.description = 'idle';
            verNode.iconPath    = new vscode.ThemeIcon('check');
        }
        items.push(verNode);

        return items;
    }

    private _buildRootNode(name: string, winPath: string): CodesearchTreeItem {
        const docs     = this._docCounts[name];
        const docsStr  = docs !== undefined ? `${fmtDocs(docs)} docs` : '';
        const spinning = this._detail.verifierRunning || this._detail.queueDepth > 0;

        const node = new CodesearchTreeItem(
            'root', name, vscode.TreeItemCollapsibleState.None, name,
        );
        node.description = winPath + (docsStr ? `  ${docsStr}` : '');
        node.iconPath    = new vscode.ThemeIcon(spinning ? 'sync~spin' : 'folder');
        node.tooltip     = new vscode.MarkdownString(
            `**${name}**\n\n` +
            `Path: \`${winPath}\`\n\n` +
            (docs !== undefined ? `Indexed documents: **${docs.toLocaleString()}**` : 'Doc count unavailable'),
        );
        return node;
    }
}

function fmtDocs(n: number): string {
    if (n >= 1_000_000) { return `${(n / 1_000_000).toFixed(1)}M`; }
    if (n >= 1_000)     { return `${Math.round(n / 1_000)}k`; }
    return String(n);
}
