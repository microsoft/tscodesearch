import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

import {
    CodesearchConfig,
    MatchItem,
    MODES,
    loadConfig,
    getRoots,
    runSearchPipeline,
    resolveFilePath,
} from './client';
import { BUILD_DATE } from './buildInfo';
import { FileWatcher } from './watcher';
import { StatusBarManager } from './status';
import { ServerManager } from './server';
import { RootsTreeProvider, CodesearchTreeItem } from './treeview';

// ---------------------------------------------------------------------------
// Config discovery (needs vscode API)
// ---------------------------------------------------------------------------

function friendlyConfigError(raw: string): string {
    if (raw.includes('directory, not a file') || raw.includes('EISDIR')) {
        return `tscodesearch.configPath points to a directory — set it to the config.json file itself (e.g. C:\\myproject\\codesearch\\config.json)`;
    }
    if (raw.includes('not found') || raw.includes('ENOENT')) {
        return `config.json not found at the configured path — check the tscodesearch.configPath setting`;
    }
    if (raw.includes('JSON') || raw.includes('Unexpected token') || raw.includes('SyntaxError')) {
        return `config.json contains invalid JSON — check the file for syntax errors`;
    }
    return `Failed to load config.json — ${raw}`;
}

function findConfigPath(): { found: string | null; searched: string[] } {
    const setting = vscode.workspace.getConfiguration('tscodesearch').get<string>('configPath');
    if (setting) {
        try {
            const stat = fs.statSync(setting);
            if (!stat.isFile()) {
                throw new Error(`tscodesearch.configPath points to a directory, not a file.\nExpected a path like: ${path.join(setting, 'config.json')}`);
            }
        } catch (e: unknown) {
            if ((e as NodeJS.ErrnoException).code === 'ENOENT') {
                throw new Error(`tscodesearch.configPath not found: ${setting}`);
            }
            throw e;
        }
        return { found: setting, searched: [] };
    }

    const searched: string[] = [];

    // Build search roots: repoPath first (most likely location), then workspace folders
    const repoPath = vscode.workspace.getConfiguration('tscodesearch').get<string>('repoPath');
    const searchRoots: string[] = [];
    if (repoPath) { searchRoots.push(repoPath); }
    for (const folder of vscode.workspace.workspaceFolders || []) {
        searchRoots.push(folder.uri.fsPath);
    }

    for (const root of searchRoots) {
        for (const rel of ['config.json', 'codesearch/config.json']) {
            const candidate = path.join(root, rel);
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
<title>TsCodeSearch</title>
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
/* --- capped results --- */
.cap-hint{padding:6px 10px;font-size:11px;color:var(--vscode-descriptionForeground);font-style:italic;border-bottom:1px solid var(--vscode-panel-border,#333)}
.cap-loading{padding:2px 8px 2px 24px;font-size:11px;color:var(--vscode-descriptionForeground);font-style:italic}
.cap-still-capped{padding:6px 8px 6px 24px;font-size:11px;color:var(--vscode-descriptionForeground)}
.sub-hdr.is-cap{cursor:pointer}
.sub-hdr.is-cap:hover{background:var(--vscode-list-hoverBackground)}
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

  // Current search params — needed when expanding capped subsystems
  var currentSearch = { query: '', mode: '', ext: '', sub: '', root: '' };
  // Cached facets and expansion state for capped results
  var currentFacets = [];
  var subExpansions = {}; // sub → { state: 'loading'|'loaded'|'capped', hits, found }

  function triggerSearch() {
    clearTimeout(timer);
    timer = setTimeout(function() {
      var query = qEl.value.trim();
      if (!query) {
        resultsEl.innerHTML = '<div class="empty">Type to search across your codebase</div>';
        statusEl.textContent = 'Built: ' + BUILD_DATE; statusEl.className = 'status-bar';
        return;
      }
      subExpansions = {};
      statusEl.innerHTML = 'Searching<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
      statusEl.className = 'status-bar';
      var params = { type: 'search', query: query, mode: modeEl.value,
        ext: extEl.value.trim(), sub: subEl.value.trim(), root: rootEl.value, limit: 20 };
      currentSearch = { query: params.query, mode: params.mode, ext: params.ext,
                        sub: params.sub, root: params.root };
      vscode.postMessage(params);
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

  // ── Tree rendering helpers ────────────────────────────────────────────────

  function renderDirTree(hits) {
    // Groups hits by directory, renders dir+file+match nodes (no sub header).
    var dirs = {};
    hits.forEach(function(hit) {
      var rel = hit.document.relative_path || '';
      var slash = rel.lastIndexOf('/');
      var dir = slash >= 0 ? rel.slice(0, slash) : '';
      if (!dirs[dir]) { dirs[dir] = []; }
      dirs[dir].push(hit);
    });
    var html = '';
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
    return html;
  }

  function renderTree(hits) {
    // Groups hits by subsystem, then renders subsystem > dir > file > matches.
    var subs = {};
    hits.forEach(function(hit) {
      var sub = hit.document.subsystem || '';
      if (!subs[sub]) { subs[sub] = []; }
      subs[sub].push(hit);
    });
    var html = '';
    Object.keys(subs).sort().forEach(function(sub) {
      var subHits = subs[sub];
      html += '<div class="sub-node">';
      html += '<div class="sub-hdr"><span class="chev">&#9660;</span>';
      html += sub ? '<span class="sub-name">' + esc(sub) + '</span>'
                  : '<span class="sub-name dim">(no subsystem)</span>';
      html += '<span class="badge">' + subHits.length + '</span></div>';
      html += '<div class="sub-body">' + renderDirTree(subHits) + '</div>';
      html += '</div>';
    });
    return html;
  }

  function attachTreeHandlers() {
    resultsEl.querySelectorAll('.sub-hdr:not(.is-cap)').forEach(function(hdr) {
      hdr.addEventListener('click', function() { hdr.parentNode.classList.toggle('collapsed'); });
    });
    resultsEl.querySelectorAll('.dir-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() { hdr.parentNode.classList.toggle('collapsed'); });
    });
    resultsEl.querySelectorAll('.file-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() {
        vscode.postMessage({ type: 'openFile', relativePath: hdr.dataset.path, root: currentSearch.root, query: currentSearch.query });
      });
      hdr.addEventListener('keydown', function(e) { if (e.key === 'Enter') { hdr.click(); } });
    });
    resultsEl.querySelectorAll('.match-item').forEach(function(item) {
      item.addEventListener('click', function(e) {
        e.stopPropagation();
        var line = (item.dataset.line !== undefined && item.dataset.line !== '')
          ? parseInt(item.dataset.line, 10) : undefined;
        vscode.postMessage({ type: 'openFile', relativePath: item.dataset.path, root: currentSearch.root, line: line, query: currentSearch.query });
      });
      item.addEventListener('keydown', function(e) { if (e.key === 'Enter') { item.click(); } });
    });
  }

  // ── Capped results rendering ──────────────────────────────────────────────

  function renderCappedTree() {
    var html = '<div class="cap-hint">Too many results \u2014 click a subsystem to expand</div>';
    currentFacets.forEach(function(f) {
      var sub = f.value;
      var exp = subExpansions[sub];
      var isOpen = exp && exp.state !== 'idle';
      html += '<div class="sub-node" id="capped-sub-' + esc(sub) + '">';
      html += '<div class="sub-hdr is-cap" tabindex="0" data-sub="' + esc(sub) + '">';
      html += '<span class="chev" style="' + (isOpen ? '' : 'transform:rotate(-90deg)') + '">&#9660;</span>';
      html += '<span class="sub-name">' + esc(sub) + '</span>';
      html += '<span class="badge">' + f.count + '</span>';
      if (exp && exp.state === 'loading') {
        html += '<span class="cap-loading">Loading\u2026</span>';
      }
      html += '</div>';
      if (exp && exp.state === 'loaded') {
        html += '<div class="sub-body">';
        if (exp.hits.length === 0) {
          html += '<div class="cap-still-capped">No results</div>';
        } else {
          if (exp.capped) {
            html += '<div class="cap-still-capped">Showing ' + exp.hits.length + ' of ' + exp.found
                  + ' \u2014 narrow further with the Sub filter</div>';
          }
          html += renderDirTree(exp.hits);
        }
        html += '</div>';
      }
      html += '</div>';
    });
    return html;
  }

  function attachCappedHandlers() {
    resultsEl.querySelectorAll('.sub-hdr.is-cap').forEach(function(hdr) {
      var sub = hdr.dataset.sub;
      hdr.addEventListener('click', function() { handleSubExpand(sub); });
      hdr.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' || e.key === ' ') { handleSubExpand(sub); e.preventDefault(); }
      });
    });
    attachTreeHandlers(); // handles any already-expanded file/match nodes
  }

  function handleSubExpand(sub) {
    var exp = subExpansions[sub];
    if (!exp || exp.state === 'idle') {
      subExpansions[sub] = { state: 'loading' };
      refreshCappedNode(sub);
      vscode.postMessage({ type: 'expandSub', sub: sub,
        query: currentSearch.query, mode: currentSearch.mode,
        ext: currentSearch.ext, root: currentSearch.root });
    } else {
      // Toggle collapse on already-loaded node
      var node = document.getElementById('capped-sub-' + sub);
      if (node) { node.classList.toggle('collapsed'); }
    }
  }

  function refreshCappedNode(sub) {
    // Re-render just this subsystem node in place
    var node = document.getElementById('capped-sub-' + sub);
    if (!node) { return; }
    var exp = subExpansions[sub];
    var isOpen = exp && exp.state !== 'idle';
    var hdr = node.querySelector('.sub-hdr.is-cap');
    if (hdr) {
      var chev = hdr.querySelector('.chev');
      if (chev) { chev.style.transform = isOpen ? '' : 'rotate(-90deg)'; }
      // Update loading indicator
      var existing = hdr.querySelector('.cap-loading');
      if (exp && exp.state === 'loading') {
        if (!existing) {
          var s = document.createElement('span');
          s.className = 'cap-loading'; s.textContent = 'Loading\u2026';
          hdr.appendChild(s);
        }
      } else if (existing) {
        existing.remove();
      }
    }
    // Update body
    var body = node.querySelector('.sub-body');
    if (exp && exp.state === 'loaded') {
      if (!body) { body = document.createElement('div'); body.className = 'sub-body'; node.appendChild(body); }
      var inner = '';
      if (exp.capped) {
        inner += '<div class="cap-still-capped">Showing ' + exp.hits.length + ' of ' + exp.found
               + ' \u2014 narrow further with the Sub filter</div>';
      }
      inner += exp.hits.length === 0 ? '<div class="cap-still-capped">No results</div>' : renderDirTree(exp.hits);
      body.innerHTML = inner;
      attachTreeHandlers();
    } else if (body) {
      body.remove();
    }
  }

  // ── Main result display ───────────────────────────────────────────────────

  function showResults(data) {
    hideConfigError();
    var hits = data.hits || [];
    var found = data.found || 0;
    var isCapped = found > hits.length && found > 0;
    var modeLabel = MODES.find(function(m) { return m.key === data.mode; });
    modeLabel = modeLabel ? modeLabel.label : data.mode;

    if (isCapped) {
      statusEl.textContent = found + ' files matched \u2014 too many to show all \u2014 ' + modeLabel + ' mode';
    } else {
      statusEl.textContent = found === 0
        ? 'No results'
        : found + ' result' + (found === 1 ? '' : 's') + ' \u2014 ' + data.elapsed + 'ms \u2014 ' + modeLabel + ' mode';
    }
    statusEl.className = 'status-bar';

    if (found === 0) {
      resultsEl.innerHTML = '<div class="empty">No results for <strong>' + esc(data.query) + '</strong></div>';
      return;
    }

    if (isCapped) {
      // Build facet list from the data; fall back to whatever hits we got
      currentFacets = [];
      if (data.facet_counts) {
        var subFacet = data.facet_counts.find(function(f) { return f.field_name === 'subsystem'; });
        if (subFacet) { currentFacets = subFacet.counts || []; }
      }
      if (currentFacets.length === 0) {
        // Synthesize from hits we received
        var seen = {};
        hits.forEach(function(h) { var s = h.document.subsystem || ''; seen[s] = (seen[s] || 0) + 1; });
        currentFacets = Object.keys(seen).sort().map(function(s) { return { value: s, count: seen[s] }; });
      }
      resultsEl.innerHTML = renderCappedTree();
      attachCappedHandlers();
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
    else if (msg.type === 'subResults') {
      var sub = msg.sub;
      if (msg.error) {
        subExpansions[sub] = { state: 'loaded', hits: [], found: 0, capped: false };
      } else {
        var hits = msg.hits || [];
        var found = msg.found || 0;
        subExpansions[sub] = { state: 'loaded', hits: hits, found: found, capped: found > hits.length };
      }
      refreshCappedNode(sub);
    }
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
    public static readonly viewType = 'tscodesearch.panel';

    private _view?: vscode.WebviewView;
    private _config: CodesearchConfig | null = null;
    private _roots: string[] = ['default'];
    private _defaultRoot = 'default';

    constructor(
        private readonly _docker: ServerManager,
        private readonly _out:   vscode.OutputChannel,
    ) {}

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
            vscode.commands.executeCommand('workbench.action.openSettings', 'tscodesearch.configPath');
        }

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'openSettings') {
                vscode.commands.executeCommand('workbench.action.openSettings', 'tscodesearch.configPath');

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
                        facet_counts: pr.facet_counts ?? [],
                    });
                } catch (e: unknown) {
                    const msg = e instanceof Error ? e.message : String(e);
                    this._out.appendLine(`[search] Error: ${msg}`);
                    webviewView.webview.postMessage({ type: 'error', message: msg });
                }

            } else if (msg.type === 'expandSub') {
                if (!this._config && !this._reloadConfig()) { return; }
                try {
                    const pr = await runSearchPipeline(
                        this._config!, msg.query as string, msg.mode as string,
                        msg.ext || '', msg.sub as string,
                        msg.root || this._defaultRoot, 50,
                    );
                    webviewView.webview.postMessage({
                        type: 'subResults',
                        sub: msg.sub,
                        hits: pr.hits,
                        found: pr.found,
                        elapsed: pr.elapsed,
                    });
                } catch (e: unknown) {
                    webviewView.webview.postMessage({
                        type: 'subResults',
                        sub: msg.sub,
                        error: e instanceof Error ? e.message : String(e),
                    });
                }

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
            }
        });
    }

    private _reloadConfig(): boolean {
        try {
            // Docker mode: use settings-based roots when configured
            if (this._docker.mode === 'docker') {
                const configuredRoots = this._docker.getRoots();
                if (Object.keys(configuredRoots).length > 0) {
                    this._config = this._docker.getClientConfig();
                    const rootMap = getRoots(this._config);
                    this._roots = Object.keys(rootMap);
                    this._defaultRoot = this._roots[0] ?? 'default';
                    this._out.appendLine(`[config] Loaded from settings — roots: ${this._roots.join(', ')} | port: ${this._config.port}`);
                    return true;
                }
            }

            // WSL mode (or docker without configured roots): discover config.json on disk
            const { found, searched } = findConfigPath();
            if (!found) {
                const lines = [
                    'config.json not found.',
                    '',
                    'Run "TsCodeSearch: Set Up" to configure the server,',
                    'or set tscodesearch.configPath to the full path of config.json.',
                    'Example: C:\\myproject\\codesearch\\config.json',
                ];
                if (searched.length > 0) {
                    lines.push('', 'Locations searched:');
                    searched.forEach((p) => lines.push(`  • ${p}`));
                }
                this._out.appendLine(`[config] Not found. Searched: ${searched.join(', ')}`);
                this._view?.webview.postMessage({ type: 'configError', message: lines.join('\n') });
                return false;
            }
            this._config = loadConfig(found);
            const rootMap = getRoots(this._config);
            this._roots = Object.keys(rootMap);
            this._defaultRoot = this._roots[0] ?? 'default';
            this._out.appendLine(`[config] Loaded ${found} — roots: ${this._roots.join(', ')} | port: ${this._config.port}`);
            return true;
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            this._out.appendLine(`[config] Error: ${msg}`);
            this._view?.webview.postMessage({ type: 'configError', message: friendlyConfigError(msg) });
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
            watcher = new FileWatcher(config);
            context.subscriptions.push(watcher);
            context.subscriptions.push(new StatusBarManager(watcher, treeProvider));
        } catch { /* non-fatal */ }
    }

    if (docker.mode === 'wsl') {
        // WSL mode: config.json is the source of truth for API key, port, and roots.
        try {
            const { found } = findConfigPath();
            if (found) {
                docker.setDiskConfig(loadConfig(found));
                _startWatcherAndStatus(docker.getClientConfig());
            }
        } catch { /* non-fatal */ }
    } else {
        // Docker mode: VS Code settings own the config.
        const configuredRoots = docker.getRoots();
        if (Object.keys(configuredRoots).length > 0) {
            _startWatcherAndStatus(docker.getClientConfig());
        }
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
            await docker.addRoot(name.trim(), picks[0].fsPath);
            treeProvider.refresh();
            const choice = await vscode.window.showInformationMessage(
                `TsCodeSearch: Added root "${name.trim()}". Restart the server to apply.`,
                'Restart Now',
            );
            if (choice === 'Restart Now') {
                void vscode.commands.executeCommand('tscodesearch.restartContainer');
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
            await docker.removeRoot(name);
            treeProvider.refresh();
        }),
    );

    // Restart the server
    context.subscriptions.push(
        vscode.commands.registerCommand('tscodesearch.restartContainer', () => {
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
        vscode.commands.registerCommand('tscodesearch.stopContainer', () => {
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
            void (watcher ?? new FileWatcher(docker.getClientConfig()))
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
