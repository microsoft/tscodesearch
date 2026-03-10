import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

import {
    CodesearchConfig,
    TypesenseHit,
    MODES,
    loadConfig,
    getRoots,
    doSearch,
    queryAst,
    resolveFilePath,
} from './client';
import { BUILD_DATE } from './buildInfo';

// ---------------------------------------------------------------------------
// Per-file match items (shown as tree leaves)
// ---------------------------------------------------------------------------

interface MatchItem { text: string; line?: number; }

function computeMatchItems(
    hit: TypesenseHit,
    mode: string,
): MatchItem[] {
    const doc = hit.document;
    const hl = hit.highlights ?? [];
    switch (mode) {
        case 'text': {
            for (const h of hl) {
                if (h.field === 'content') {
                    const s = h.snippet ?? h.snippets?.[0];
                    if (s) { return [{ text: s.replace(/<\/?mark>/g, '').trim() }]; }
                }
            }
            return (doc.method_names ?? []).slice(0, 6).map((n) => ({ text: n }));
        }
        case 'symbols':
            return [
                ...(doc.class_names ?? []).slice(0, 3).map((n) => ({ text: n })),
                ...(doc.method_names ?? []).slice(0, 6).map((n) => ({ text: n })),
            ].slice(0, 8);
        case 'implements':
            return (doc.base_types ?? []).map((t) => ({ text: t }));
        case 'sig':
            return (doc.method_sigs ?? []).slice(0, 5).map((s) => ({ text: s }));
        case 'uses':
            return (doc.type_refs ?? []).slice(0, 8).map((t) => ({ text: t }));
        case 'attr':
            return (doc.attributes ?? []).map((a) => ({ text: a }));
        default:
            return [];
    }
}

// ---------------------------------------------------------------------------
// Config discovery (needs vscode API)
// ---------------------------------------------------------------------------

function friendlyConfigError(raw: string): string {
    if (raw.includes('directory, not a file') || raw.includes('EISDIR')) {
        return `codesearch.configPath points to a directory — set it to the config.json file itself (e.g. C:\\myproject\\codesearch\\config.json)`;
    }
    if (raw.includes('not found') || raw.includes('ENOENT')) {
        return `config.json not found at the configured path — check the codesearch.configPath setting`;
    }
    if (raw.includes('JSON') || raw.includes('Unexpected token') || raw.includes('SyntaxError')) {
        return `config.json contains invalid JSON — check the file for syntax errors`;
    }
    return `Failed to load config.json — ${raw}`;
}

function findConfigPath(): { found: string | null; searched: string[] } {
    const setting = vscode.workspace.getConfiguration('codesearch').get<string>('configPath');
    if (setting) {
        try {
            const stat = fs.statSync(setting);
            if (!stat.isFile()) {
                throw new Error(`codesearch.configPath points to a directory, not a file.\nExpected a path like: ${path.join(setting, 'config.json')}`);
            }
        } catch (e: unknown) {
            if ((e as NodeJS.ErrnoException).code === 'ENOENT') {
                throw new Error(`codesearch.configPath not found: ${setting}`);
            }
            throw e;
        }
        return { found: setting, searched: [] };
    }

    const searched: string[] = [];
    for (const folder of vscode.workspace.workspaceFolders || []) {
        for (const rel of ['codesearch/config.json', 'config.json']) {
            const candidate = path.join(folder.uri.fsPath, rel);
            searched.push(candidate);
            if (!fs.existsSync(candidate)) { continue; }
            try {
                const d = JSON.parse(fs.readFileSync(candidate, 'utf-8'));
                if ('api_key' in d && ('roots' in d || 'src_root' in d)) {
                    return { found: candidate, searched };
                }
            } catch { /* skip */ }
        }
    }
    return { found: null, searched };
}

// ---------------------------------------------------------------------------
// Nonce helper
// ---------------------------------------------------------------------------

function getNonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    return Array.from({ length: 32 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

// ---------------------------------------------------------------------------
// Webview HTML
// ---------------------------------------------------------------------------

function buildWebviewHtml(nonce: string, roots: string[], defaultRoot: string): string {
    const modesJson  = JSON.stringify(MODES.map((m) => ({ key: m.key, label: m.label, desc: m.desc })));
    const rootsJson  = JSON.stringify(roots);
    const defaultRootJson = JSON.stringify(defaultRoot);
    const buildDateJson   = JSON.stringify(BUILD_DATE);

    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Code Search</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--vscode-font-family);font-size:var(--vscode-font-size);color:var(--vscode-foreground);background:var(--vscode-editor-background);height:100vh;display:flex;flex-direction:column;overflow:hidden}
.header{padding:8px;border-bottom:1px solid var(--vscode-panel-border,#333);flex-shrink:0;display:flex;flex-direction:column;gap:6px}
.search-row{display:flex;gap:4px;align-items:center}
input.search-box{flex:1;background:var(--vscode-input-background);color:var(--vscode-input-foreground);border:1px solid var(--vscode-input-border,transparent);border-radius:2px;padding:5px 8px;font-family:inherit;font-size:inherit;outline:none}
input.search-box:focus{border-color:var(--vscode-focusBorder)}
input.search-box::placeholder{color:var(--vscode-input-placeholderForeground)}
.filters{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center}
.filter-label{font-size:11px;color:var(--vscode-descriptionForeground);white-space:nowrap}
select.filter-select,input.filter-input{background:var(--vscode-dropdown-background,var(--vscode-input-background));color:var(--vscode-dropdown-foreground,var(--vscode-input-foreground));border:1px solid var(--vscode-dropdown-border,var(--vscode-input-border,transparent));border-radius:2px;padding:3px 6px;font-family:inherit;font-size:11px;outline:none;cursor:pointer}
select.filter-select:focus,input.filter-input:focus{border-color:var(--vscode-focusBorder)}
input.filter-input{width:90px;cursor:text}
input.filter-input::placeholder{color:var(--vscode-input-placeholderForeground);font-style:italic}
.status-bar{padding:3px 10px;font-size:11px;color:var(--vscode-descriptionForeground);min-height:20px;flex-shrink:0;border-bottom:1px solid var(--vscode-panel-border,#333)}
.status-bar.error{color:var(--vscode-errorForeground)}
.results{flex:1;overflow-y:auto;padding-bottom:8px}
.empty{padding:32px 20px;text-align:center;color:var(--vscode-descriptionForeground);font-size:13px}
.badge{display:inline-block;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground);border-radius:8px;padding:0 6px;font-size:10px;font-weight:600;vertical-align:middle;margin-left:5px}
/* --- tree --- */
.sub-node,.dir-node{user-select:none}
.sub-hdr,.dir-hdr{display:flex;align-items:center;gap:5px;padding:5px 8px;cursor:pointer}
.sub-hdr:hover,.dir-hdr:hover{background:var(--vscode-list-hoverBackground)}
.sub-hdr{font-weight:600;border-top:1px solid var(--vscode-panel-border,#333)}
.sub-hdr:first-child{border-top:none}
.dir-hdr{padding-left:20px;font-size:calc(var(--vscode-font-size) - 1px);color:var(--vscode-descriptionForeground)}
.chev{font-size:9px;opacity:.6;flex-shrink:0;width:10px;display:inline-block;transition:transform .12s}
.sub-node.collapsed>.sub-body,.dir-node.collapsed>.dir-body{display:none}
.sub-node.collapsed>.sub-hdr>.chev,.dir-node.collapsed>.dir-hdr>.chev{transform:rotate(-90deg)}
.sub-name{flex:1}
.dir-name{flex:1;font-family:var(--vscode-editor-font-family,monospace);font-size:10.5px}
.file-hdr{display:flex;align-items:center;gap:5px;padding:3px 8px 3px 34px;cursor:pointer;outline:none}
.file-hdr:hover,.file-hdr:focus{background:var(--vscode-list-hoverBackground)}
.file-name{font-weight:600}
.match-item{display:flex;align-items:baseline;gap:3px;padding:2px 8px 2px 50px;cursor:pointer;font-size:11px;outline:none}
.match-item:hover,.match-item:focus{background:var(--vscode-list-hoverBackground)}
.tree-branch{color:var(--vscode-descriptionForeground);opacity:.4;flex-shrink:0;font-family:monospace;font-size:11px}
.match-text{font-family:var(--vscode-editor-font-family,monospace);opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.match-line{color:var(--vscode-descriptionForeground);opacity:.55;font-size:10px;flex-shrink:0;margin-left:2px}
.dim{opacity:.5;font-style:italic}
/* --- config error --- */
.config-error{display:none;flex-direction:column;align-items:center;justify-content:center;gap:16px;flex:1;padding:32px 24px;text-align:center}
.config-error-msg{color:var(--vscode-errorForeground);font-size:13px;max-width:420px;line-height:1.6;white-space:pre-wrap;text-align:left}
.config-error-btn{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:none;border-radius:2px;padding:6px 14px;font-family:inherit;font-size:13px;cursor:pointer}
.config-error-btn:hover{background:var(--vscode-button-hoverBackground)}
@keyframes blink{0%,80%,100%{opacity:0}40%{opacity:1}}
.dot{display:inline-block;animation:blink 1.4s infinite}
.dot:nth-child(2){animation-delay:.2s}
.dot:nth-child(3){animation-delay:.4s}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--vscode-scrollbarSlider-background);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--vscode-scrollbarSlider-hoverBackground)}
</style>
</head>
<body>
<div class="header">
  <div class="search-row">
    <input id="q" class="search-box" type="text" placeholder="Search code…" spellcheck="false" autocomplete="off">
  </div>
  <div class="filters">
    <span class="filter-label">Mode</span>
    <select id="mode" class="filter-select" title="Search mode"></select>
    <span class="filter-label">Ext</span>
    <input id="ext" class="filter-input" type="text" placeholder="cs, h, py…" title="Filter by file extension (e.g. cs)">
    <span class="filter-label">Sub</span>
    <input id="sub" class="filter-input" type="text" placeholder="subsystem…" title="Filter by subsystem directory">
    <span id="rootWrap" class="filter-label" style="display:none">Root</span>
    <select id="root" class="filter-select" title="Source root" style="display:none"></select>
  </div>
</div>
<div id="status" class="status-bar"></div>
<div id="results" class="results">
  <div class="empty">Type to search across your codebase</div>
</div>
<div id="configError" class="config-error">
  <div id="configErrorMsg" class="config-error-msg"></div>
  <button class="config-error-btn" id="configErrorBtn">Open Settings</button>
</div>
<script nonce="${nonce}">
(function() {
  'use strict';
  const vscode = acquireVsCodeApi();
  const MODES = ${modesJson};
  const ROOTS = ${rootsJson};
  const DEFAULT_ROOT = ${defaultRootJson};
  const BUILD_DATE = ${buildDateJson};

  const modeEl = document.getElementById('mode');
  MODES.forEach(function(m) {
    var o = document.createElement('option');
    o.value = m.key; o.textContent = m.label; o.title = m.desc;
    modeEl.appendChild(o);
  });

  const rootEl = document.getElementById('root');
  const rootWrap = document.getElementById('rootWrap');
  ROOTS.forEach(function(r) {
    var o = document.createElement('option');
    o.value = r; o.textContent = r;
    if (r === DEFAULT_ROOT) { o.selected = true; }
    rootEl.appendChild(o);
  });
  if (ROOTS.length > 1) { rootEl.style.display = ''; rootWrap.style.display = ''; }

  var timer = null;
  const qEl = document.getElementById('q');
  const extEl = document.getElementById('ext');
  const subEl = document.getElementById('sub');
  const statusEl = document.getElementById('status');
  const resultsEl = document.getElementById('results');
  const configErrorEl = document.getElementById('configError');
  const configErrorMsgEl = document.getElementById('configErrorMsg');

  document.getElementById('configErrorBtn').addEventListener('click', function() {
    vscode.postMessage({ type: 'openSettings' });
  });

  function showConfigError(message) {
    statusEl.textContent = ''; statusEl.className = 'status-bar';
    resultsEl.style.display = 'none';
    configErrorMsgEl.textContent = message; configErrorEl.style.display = 'flex';
  }
  function hideConfigError() { configErrorEl.style.display = 'none'; resultsEl.style.display = ''; }

  function triggerSearch() {
    clearTimeout(timer);
    timer = setTimeout(function() {
      var query = qEl.value.trim();
      if (!query) {
        resultsEl.innerHTML = '<div class="empty">Type to search across your codebase</div>';
        statusEl.textContent = 'Built: ' + BUILD_DATE; statusEl.className = 'status-bar';
        return;
      }
      statusEl.innerHTML = 'Searching<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
      statusEl.className = 'status-bar';
      vscode.postMessage({ type: 'search', query: query, mode: modeEl.value,
        ext: extEl.value.trim(), sub: subEl.value.trim(), root: rootEl.value, limit: 20 });
    }, 180);
  }

  qEl.addEventListener('input', triggerSearch);
  modeEl.addEventListener('change', triggerSearch);
  extEl.addEventListener('input', triggerSearch);
  subEl.addEventListener('input', triggerSearch);
  rootEl.addEventListener('change', triggerSearch);

  function esc(s) {
    if (!s) { return ''; }
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function buildTree(hits) {
    var subs = {};
    hits.forEach(function(hit) {
      var doc = hit.document;
      var sub = doc.subsystem || '';
      var rel = doc.relative_path || '';
      var slash = rel.lastIndexOf('/');
      var dir = slash >= 0 ? rel.slice(0, slash) : '';
      if (!subs[sub]) { subs[sub] = {}; }
      if (!subs[sub][dir]) { subs[sub][dir] = []; }
      subs[sub][dir].push(hit);
    });
    return subs;
  }

  function renderTree(hits) {
    var subs = buildTree(hits);
    var html = '';
    Object.keys(subs).sort().forEach(function(sub) {
      var dirs = subs[sub];
      var fileCount = Object.keys(dirs).reduce(function(n, d) { return n + dirs[d].length; }, 0);
      html += '<div class="sub-node">';
      html += '<div class="sub-hdr"><span class="chev">&#9660;</span>';
      html += sub ? '<span class="sub-name">' + esc(sub) + '</span>'
                  : '<span class="sub-name dim">(no subsystem)</span>';
      html += '<span class="badge">' + fileCount + '</span></div>';
      html += '<div class="sub-body">';
      Object.keys(dirs).sort().forEach(function(dir) {
        var files = dirs[dir];
        html += '<div class="dir-node">';
        html += '<div class="dir-hdr"><span class="chev">&#9660;</span>';
        html += '<span class="dir-name">' + esc(dir ? dir + '/' : '(root)') + '</span></div>';
        html += '<div class="dir-body">';
        files.forEach(function(hit) {
          var doc = hit.document;
          var matches = hit._matches || [];
          var fname = doc.filename || (doc.relative_path || '').split('/').pop() || '';
          html += '<div class="file-node">';
          html += '<div class="file-hdr" tabindex="0" data-path="' + esc(doc.relative_path) + '">';
          html += '<span class="file-name">' + esc(fname) + '</span></div>';
          if (matches.length > 0) {
            html += '<div class="file-body">';
            matches.forEach(function(m, i) {
              var last = (i === matches.length - 1);
              var br = last ? '\u2514\u2500' : '\u251c\u2500';
              var la = (m.line !== undefined && m.line !== null) ? ' data-line="' + m.line + '"' : '';
              html += '<div class="match-item" tabindex="0" data-path="' + esc(doc.relative_path) + '"' + la + '>';
              html += '<span class="tree-branch">' + br + '</span>';
              html += '<span class="match-text">' + esc(m.text) + '</span>';
              if (m.line !== undefined && m.line !== null) {
                html += '<span class="match-line">:' + (m.line + 1) + '</span>';
              }
              html += '</div>';
            });
            html += '</div>';
          }
          html += '</div>';
        });
        html += '</div></div>'; // dir-body, dir-node
      });
      html += '</div></div>'; // sub-body, sub-node
    });
    return html;
  }

  function attachTreeHandlers() {
    resultsEl.querySelectorAll('.sub-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() { hdr.parentNode.classList.toggle('collapsed'); });
    });
    resultsEl.querySelectorAll('.dir-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() { hdr.parentNode.classList.toggle('collapsed'); });
    });
    resultsEl.querySelectorAll('.file-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() {
        vscode.postMessage({ type: 'openFile', relativePath: hdr.dataset.path, root: rootEl.value, query: qEl.value.trim() });
      });
      hdr.addEventListener('keydown', function(e) { if (e.key === 'Enter') { hdr.click(); } });
    });
    resultsEl.querySelectorAll('.match-item').forEach(function(item) {
      item.addEventListener('click', function(e) {
        e.stopPropagation();
        var line = (item.dataset.line !== undefined && item.dataset.line !== '')
          ? parseInt(item.dataset.line, 10) : undefined;
        vscode.postMessage({ type: 'openFile', relativePath: item.dataset.path, root: rootEl.value, line: line, query: qEl.value.trim() });
      });
      item.addEventListener('keydown', function(e) { if (e.key === 'Enter') { item.click(); } });
    });
  }

  function showResults(data) {
    hideConfigError();
    var hits = data.hits || [];
    var found = data.found || 0;
    var modeLabel = MODES.find(function(m) { return m.key === data.mode; });
    modeLabel = modeLabel ? modeLabel.label : data.mode;
    statusEl.textContent = found === 0
      ? 'No results'
      : found + ' result' + (found === 1 ? '' : 's') + ' \u2014 ' + data.elapsed + 'ms \u2014 ' + modeLabel + ' mode';
    statusEl.className = 'status-bar';
    if (hits.length === 0) {
      resultsEl.innerHTML = '<div class="empty">No results for <strong>' + esc(data.query) + '</strong></div>';
      return;
    }
    resultsEl.innerHTML = renderTree(hits);
    attachTreeHandlers();
  }

  qEl.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') { var f = resultsEl.querySelector('.file-hdr,.match-item'); if (f) { f.focus(); e.preventDefault(); } }
  });

  window.addEventListener('message', function(ev) {
    var msg = ev.data;
    if (msg.type === 'results') { showResults(msg); }
    else if (msg.type === 'error') {
      statusEl.textContent = 'Error: ' + msg.message;
      statusEl.className = 'status-bar error';
      resultsEl.innerHTML = '<div class="empty">Search failed \u2014 is the Typesense server running?<br><small>Run: ts start</small></div>';
    } else if (msg.type === 'configError') { showConfigError(msg.message); }
  });

  statusEl.textContent = 'Built: ' + BUILD_DATE;
  qEl.focus();
})();
</script>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// WebviewView provider
// ---------------------------------------------------------------------------

class CodesearchViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'codesearch.panel';

    private _view?: vscode.WebviewView;
    private _config: CodesearchConfig | null = null;
    private _roots: string[] = ['default'];
    private _defaultRoot = 'default';

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
            vscode.commands.executeCommand('workbench.action.openSettings', 'codesearch.configPath');
        }

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'openSettings') {
                vscode.commands.executeCommand('workbench.action.openSettings', 'codesearch.configPath');

            } else if (msg.type === 'search') {
                if (!this._config) {
                    const ok = this._reloadConfig();
                    if (!ok) { return; }
                    // Rebuild HTML in case roots changed now that config loaded
                    webviewView.webview.html = buildWebviewHtml(getNonce(), this._roots, this._defaultRoot);
                }
                const start = Date.now();
                try {
                    const result = await doSearch(
                        this._config!, msg.query, msg.mode,
                        msg.ext || '', msg.sub || '',
                        msg.root || this._defaultRoot, msg.limit || 20,
                    );
                    const rootMap = getRoots(this._config!);
                    const rootPath = rootMap[msg.root || this._defaultRoot] ?? Object.values(rootMap)[0];
                    const rawHits = result.hits ?? [];
                    let hits: Array<TypesenseHit & { _matches: MatchItem[] }>;
                    if ((msg.mode === 'callers' || msg.mode === 'sig') && rootPath) {
                        const filePaths = rawHits.map((h) =>
                            resolveFilePath(rootPath, h.document.relative_path));
                        // callers → 'calls' (find call sites of the method)
                        // sig     → 'ident' (find every identifier occurrence of the pattern,
                        //           server-side filtered — mirrors query_ast ident mode in MCP)
                        const qMode = msg.mode === 'callers' ? 'calls' : 'ident';
                        const qr = await queryAst(this._config!, qMode, msg.query as string, filePaths);
                        const byFile = new Map(qr.map((r) => [r.file, r.matches]));
                        hits = rawHits.map((h) => {
                            const fp = resolveFilePath(rootPath, h.document.relative_path);
                            return {
                                ...h,
                                _matches: (byFile.get(fp) ?? []).map((m) => ({
                                    text: m.text,
                                    line: m.line - 1,   // 1-indexed → 0-indexed
                                })),
                            };
                        });
                    } else {
                        hits = rawHits.map((h) => ({ ...h, _matches: computeMatchItems(h, msg.mode as string) }));
                    }
                    webviewView.webview.postMessage({
                        type: 'results',
                        hits,
                        found: result.found ?? 0,
                        elapsed: Date.now() - start,
                        query: msg.query,
                        mode: msg.mode,
                    });
                } catch (e: unknown) {
                    webviewView.webview.postMessage({ type: 'error', message: e instanceof Error ? e.message : String(e) });
                }

            } else if (msg.type === 'openFile') {
                if (!this._config && !this._reloadConfig()) { return; }
                const rootMap = getRoots(this._config!);
                const rootPath = rootMap[msg.root as string] ?? Object.values(rootMap)[0];
                if (!rootPath) { vscode.window.showErrorMessage('Code Search: no source root configured.'); return; }
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
                    vscode.window.showErrorMessage(`Code Search: cannot open file — ${e instanceof Error ? e.message : e}`);
                }
            }
        });
    }

    private _reloadConfig(): boolean {
        try {
            const { found, searched } = findConfigPath();
            if (!found) {
                const lines = [
                    'config.json not found.',
                    '',
                    'Set codesearch.configPath to the full path of the file, including the filename.',
                    'Example: C:\\myproject\\codesearch\\config.json',
                ];
                if (searched.length > 0) {
                    lines.push('', 'Locations searched:');
                    searched.forEach((p) => lines.push(`  • ${p}`));
                }
                this._view?.webview.postMessage({ type: 'configError', message: lines.join('\n') });
                return false;
            }
            this._config = loadConfig(found);
            const rootMap = getRoots(this._config);
            this._roots = Object.keys(rootMap);
            this._defaultRoot = this._roots[0] ?? 'default';
            return true;
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            this._view?.webview.postMessage({ type: 'configError', message: friendlyConfigError(msg) });
            return false;
        }
    }
}

// ---------------------------------------------------------------------------
// Extension activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
    const provider = new CodesearchViewProvider();

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(CodesearchViewProvider.viewType, provider, {
            webviewOptions: { retainContextWhenHidden: true },
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codesearch.openPanel', () => {
            vscode.commands.executeCommand('codesearch.panel.focus');
        }),
    );
}

export function deactivate(): void { /* nothing */ }
