/**
 * Webview HTML generator.
 *
 * Pure logic — no `vscode` import at the extension-host level. The embedded
 * inline script references `vscode` (the webview API via `acquireVsCodeApi()`)
 * but that's a string baked into the HTML, not an import. Keeping this file
 * vscode-free means the rendered HTML can be unit-tested.
 */
import { MODES } from './client';
import { BUILD_DATE } from './buildInfo';

// ---------------------------------------------------------------------------
// Nonce helper
// ---------------------------------------------------------------------------

export function getNonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    return Array.from({ length: 32 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

// ---------------------------------------------------------------------------
// Webview HTML
// ---------------------------------------------------------------------------

export function buildWebviewHtml(nonce: string, roots: string[], defaultRoot: string): string {
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
.results{flex:1;overflow-y:auto;padding-bottom:8px;position:relative}
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
/* virtual-scroll row collapse chevron */
.sub-hdr.chev-collapsed>.chev,.dir-hdr.chev-collapsed>.chev{transform:rotate(-90deg)}
.sub-name{flex:1}
.dir-name{flex:1;font-family:var(--vscode-editor-font-family,monospace);font-size:10.5px}
.file-hdr{display:flex;align-items:center;gap:5px;padding:3px 8px 3px 34px;cursor:pointer;outline:none}
.file-hdr:hover,.file-hdr:focus{background:var(--vscode-list-hoverBackground)}
.file-name{font-weight:600}
.match-item{display:flex;align-items:baseline;gap:3px;padding:2px 8px 2px 50px;cursor:pointer;font-size:11px;outline:none}
.match-item:hover,.match-item:focus{background:var(--vscode-list-hoverBackground)}
.file-hdr.ast-pending{opacity:.55;font-style:italic}
.file-hdr.ast-pending::after{content:" (not expanded)";font-size:10px;opacity:.7;margin-left:4px}
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
    <input id="sub" class="filter-input" type="text" placeholder="folder path…" title="Filter by ancestor folder (e.g. services or services/billing)">
    <span id="rootWrap" class="filter-label" style="display:none">Root</span>
    <select id="root" class="filter-select" title="Source root" style="display:none"></select>
  </div>
</div>
<div id="status" class="status-bar"></div>
<div id="results" class="results">
</div>
<div id="configError" class="config-error">
  <div id="configErrorMsg" class="config-error-msg"></div>
  <button class="config-error-btn" id="configErrorBtn">Open Settings</button>
</div>
<script nonce="${nonce}">
(function() {
  'use strict';
  const vscode = acquireVsCodeApi();
  window.onerror = function(msg, src, line, col, err) {
    vscode.postMessage({ type: 'jsError', message: (err ? err.toString() : String(msg)) + ' (' + src + ':' + line + ':' + col + ')' });
    return false;
  };
  const MODES = ${modesJson};
  const ROOTS = ${rootsJson};
  const DEFAULT_ROOT = ${defaultRootJson};
  const BUILD_DATE = ${buildDateJson};

  // ── Dropdowns ─────────────────────────────────────────────────────────────
  const modeEl = document.getElementById('mode');
  MODES.forEach(function(m) {
    var o = document.createElement('option');
    o.value = m.key; o.textContent = m.label; o.title = m.desc;
    modeEl.appendChild(o);
  });

  const rootEl   = document.getElementById('root');
  const rootWrap = document.getElementById('rootWrap');
  ROOTS.forEach(function(r) {
    var o = document.createElement('option');
    o.value = r; o.textContent = r;
    if (r === DEFAULT_ROOT) { o.selected = true; }
    rootEl.appendChild(o);
  });
  if (ROOTS.length > 1) { rootEl.style.display = ''; rootWrap.style.display = ''; }

  const qEl          = document.getElementById('q');
  const extEl        = document.getElementById('ext');
  const subEl        = document.getElementById('sub');
  const statusEl     = document.getElementById('status');
  const resultsEl    = document.getElementById('results');
  const configErrorEl    = document.getElementById('configError');
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

  function esc(s) {
    if (!s) { return ''; }
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Persistent children of resultsEl ─────────────────────────────────────
  // _spacer sets the virtual scroll height; _msgEl shows empty/error text;
  // _cappedEl holds the capped-folder tree (rendered via innerHTML).
  var _spacer = document.createElement('div');
  _spacer.style.cssText = 'pointer-events:none;flex-shrink:0';
  resultsEl.appendChild(_spacer);

  var _msgEl = document.createElement('div');
  _msgEl.className = 'empty';
  _msgEl.textContent = 'Type to search across your codebase';
  resultsEl.appendChild(_msgEl);

  var _cappedEl = document.createElement('div');
  _cappedEl.style.display = 'none';
  resultsEl.appendChild(_cappedEl);

  // Switch the results area between modes without touching the spacer.
  function _setMode(mode) { // 'empty' | 'virtual' | 'capped'
    _msgEl.style.display    = mode === 'empty'   ? '' : 'none';
    _cappedEl.style.display = mode === 'capped'  ? '' : 'none';
    if (mode !== 'virtual') {
      _rendered.forEach(function(el) { el.remove(); });
      _rendered.clear();
      _rows = []; _visRows = []; _offsets = null;
      _spacer.style.height = '0';
    }
  }

  // ── Virtual scroll state ──────────────────────────────────────────────────
  // Row heights (px) per type — must match CSS padding/font-size values.
  var ROW_H = { sub: 36, dir: 27, file: 28, match: 20 };
  var OVERSCAN = 250; // px above/below visible area to keep rendered

  var _rows    = [];        // all row descriptors built from current hits
  var _visRows = [];        // rows visible given current collapse state
  var _offsets = null;      // Int32Array of cumulative top offsets, length = _visRows.length+1
  var _rendered = new Map();// visIndex → DOM element currently in resultsEl
  var _collapsedSubs  = Object.create(null); // sub  → true
  var _collapsedDirIds = Object.create(null); // numeric dirId → true
  var _rafPending = false;

  // Build a flat list of typed row descriptors from a hit array.
  // Dir rows carry a numeric _dirId used as a stable collapse key.
  function _buildRows(hits) {
    var rows = [];
    var nextDirId = 0;
    var subs = Object.create(null);
    hits.forEach(function(h) {
      var rel = (h.document.relative_path || '').replace(/\\\\/g, '/');
      var i = rel.indexOf('/');
      var s = i > 0 ? rel.slice(0, i) : '';
      (subs[s] || (subs[s] = [])).push(h);
    });
    Object.keys(subs).sort().forEach(function(sub) {
      rows.push({ type: 'sub', sub: sub, count: subs[sub].length });
      var dirs = Object.create(null);
      subs[sub].forEach(function(h) {
        var rel = h.document.relative_path || '';
        var idx = rel.lastIndexOf('/');
        var dir = idx >= 0 ? rel.slice(0, idx) : '';
        var key = sub + '\x01' + dir;
        if (!dirs[key]) { dirs[key] = { dir: dir, sub: sub, hits: [], id: nextDirId++ }; }
        dirs[key].hits.push(h);
      });
      Object.keys(dirs).sort().forEach(function(key) {
        var d = dirs[key];
        rows.push({ type: 'dir', dir: d.dir, sub: d.sub, dirId: d.id });
        d.hits.forEach(function(h) {
          var expanded = h.ast_expanded !== false;
          rows.push({ type: 'file', hit: h, sub: d.sub, dirId: d.id, astExpanded: expanded });
          var nm = h._matches.length;
          h._matches.forEach(function(m, mi) {
            rows.push({ type: 'match', match: m, hit: h, sub: d.sub, dirId: d.id, last: mi === nm - 1 });
          });
        });
      });
    });
    return rows;
  }

  // Recompute _visRows and _offsets from _rows + current collapse state.
  // Must clear _rendered first: all visIndices shift after collapse/expand,
  // so any cached DOM element→index mapping is invalid.
  function _computeVis() {
    _rendered.forEach(function(el) { el.remove(); });
    _rendered.clear();
    _visRows = [];
    _rows.forEach(function(r) {
      if (r.type === 'sub') { _visRows.push(r); return; }
      if (_collapsedSubs[r.sub]) { return; }
      if (r.type === 'dir') { _visRows.push(r); return; }
      if (_collapsedDirIds[r.dirId]) { return; }
      _visRows.push(r);
    });
    var off = new Int32Array(_visRows.length + 1);
    for (var i = 0; i < _visRows.length; i++) { off[i + 1] = off[i] + ROW_H[_visRows[i].type]; }
    _offsets = off;
    _spacer.style.height = off[_visRows.length] + 'px';
  }

  // Build the innerHTML for a row (no wrapper element yet).
  function _rowInner(r) {
    if (r.type === 'sub') {
      return '<span class="chev">&#9660;</span>'
        + (r.sub ? '<span class="sub-name">' + esc(r.sub) + '</span>'
                 : '<span class="sub-name dim">(root)</span>')
        + '<span class="badge">' + r.count + '</span>';
    }
    if (r.type === 'dir') {
      return '<span class="chev">&#9660;</span>'
        + '<span class="dir-name">' + esc(r.dir ? r.dir + '/' : '(root)') + '</span>';
    }
    if (r.type === 'file') {
      var fn = r.hit.document.filename || (r.hit.document.relative_path || '').split('/').pop() || '';
      return '<span class="file-name">' + esc(fn) + '</span>';
    }
    // match
    var m = r.match;
    var br = r.last ? '└─' : '├─';
    return '<span class="tree-branch">' + br + '</span>'
      + '<span class="match-text">' + esc(m.text) + '</span>'
      + (m.line != null ? '<span class="match-line">:' + (m.line + 1) + '</span>' : '');
  }

  // Create and position a DOM element for row r at pixel offset top.
  function _createEl(r, top) {
    var el = document.createElement('div');
    el.style.cssText = 'position:absolute;left:0;right:0;top:' + top + 'px';
    if (r.type === 'sub') {
      el.className = 'sub-hdr' + (_collapsedSubs[r.sub] ? ' chev-collapsed' : '');
      el.dataset.vsub = r.sub;
    } else if (r.type === 'dir') {
      el.className = 'dir-hdr' + (_collapsedDirIds[r.dirId] ? ' chev-collapsed' : '');
      el.dataset.vdirid = String(r.dirId);
    } else if (r.type === 'file') {
      el.className = 'file-hdr' + (r.astExpanded ? '' : ' ast-pending');
      el.dataset.path = r.hit.document.relative_path;
      el.setAttribute('tabindex', '0');
    } else {
      el.className = 'match-item';
      el.dataset.path = r.hit.document.relative_path;
      if (r.match.line != null) { el.dataset.line = String(r.match.line); }
      el.setAttribute('tabindex', '0');
    }
    el.innerHTML = _rowInner(r);
    return el;
  }

  function _scheduleRender() {
    if (_rafPending) { return; }
    _rafPending = true;
    requestAnimationFrame(_doRender);
  }

  function _doRender() {
    _rafPending = false;
    if (!_offsets || _visRows.length === 0) {
      _rendered.forEach(function(el) { el.remove(); });
      _rendered.clear();
      return;
    }
    var scrollTop   = resultsEl.scrollTop;
    var viewHeight  = resultsEl.clientHeight;
    var rangeTop    = Math.max(0, scrollTop - OVERSCAN);
    var rangeBottom = scrollTop + viewHeight + OVERSCAN;

    // Binary search: first row whose bottom edge is above rangeTop
    var lo = 0, hi = _visRows.length - 1, startIdx = 0;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (_offsets[mid + 1] > rangeTop) { startIdx = mid; hi = mid - 1; } else { lo = mid + 1; }
    }
    var endIdx = startIdx;
    while (endIdx < _visRows.length && _offsets[endIdx] < rangeBottom) { endIdx++; }

    // Remove rows that scrolled out of range
    _rendered.forEach(function(el, idx) {
      if (idx < startIdx || idx >= endIdx) { el.remove(); _rendered.delete(idx); }
    });
    // Add rows now in range
    for (var i = startIdx; i < endIdx; i++) {
      if (!_rendered.has(i)) {
        var el = _createEl(_visRows[i], _offsets[i]);
        resultsEl.appendChild(el);
        _rendered.set(i, el);
        var row = _visRows[i];
        if (row.type === 'file' && !row.astExpanded) {
          var rp = row.hit.document.relative_path;
          if (!_pendingExpansions[rp]) {
            _pendingExpansions[rp] = true;
            vscode.postMessage({ type: 'expandFile', relativePath: rp,
              root: currentSearch.root, query: currentSearch.query,
              mode: currentSearch.mode, epoch: _searchEpoch });
          }
        }
      }
    }
  }

  resultsEl.addEventListener('scroll', _scheduleRender);
  new ResizeObserver(_scheduleRender).observe(resultsEl);

  // ── Delegated click/keyboard handlers for virtual rows ────────────────────
  resultsEl.addEventListener('click', function(e) {
    try {
    var t = e.target;
    if (!t.closest) { return; }

    var sh = t.closest('.sub-hdr[data-vsub]');
    if (sh) {
      var sub = sh.dataset.vsub;
      _collapsedSubs[sub] = !_collapsedSubs[sub];
      sh.classList.toggle('chev-collapsed', !!_collapsedSubs[sub]);
      _computeVis(); _scheduleRender(); return;
    }
    var dh = t.closest('.dir-hdr[data-vdirid]');
    if (dh) {
      var did = Number(dh.dataset.vdirid);
      _collapsedDirIds[did] = !_collapsedDirIds[did];
      dh.classList.toggle('chev-collapsed', !!_collapsedDirIds[did]);
      _computeVis(); _scheduleRender(); return;
    }
    var fh = t.closest('.file-hdr[data-path]');
    if (fh && !fh.dataset.vsub) {
      vscode.postMessage({ type: 'openFile', relativePath: fh.dataset.path,
        root: currentSearch.root, query: currentSearch.query }); return;
    }
    var mi = t.closest('.match-item[data-path]');
    if (mi) {
      e.stopPropagation();
      var line = (mi.dataset.line !== undefined && mi.dataset.line !== '')
        ? parseInt(mi.dataset.line, 10) : undefined;
      vscode.postMessage({ type: 'openFile', relativePath: mi.dataset.path,
        root: currentSearch.root, line: line, query: currentSearch.query }); return;
    }
    // Capped folder expand (inside _cappedEl, not virtual rows)
    var ch = t.closest('.sub-hdr.is-cap');
    if (ch) { handleSubExpand(ch.dataset.sub); }
    } catch (err) { vscode.postMessage({ type: 'jsError', message: 'click handler: ' + (err instanceof Error ? err.message : String(err)) }); }
  });

  resultsEl.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter' && e.key !== ' ') { return; }
    var t = e.target;
    if (!t.closest) { return; }
    var fh = t.closest('.file-hdr[data-path]');
    if (fh) { fh.click(); e.preventDefault(); return; }
    var mi = t.closest('.match-item[data-path]');
    if (mi) { mi.click(); e.preventDefault(); }
  });

  // ── Capped results (folder-level lazy loading, rendered via innerHTML) ─
  var currentSearch  = { query: '', mode: '', ext: '', sub: '', root: '' };
  var _cappedSearch  = { query: '', mode: '', ext: '', root: '' }; // params from the search that produced the capped view
  var currentFacets  = [];
  var subExpansions  = {}; // sub → { state: 'loading'|'loaded', hits, found, capped }

  // ── Per-file lazy expansion (virtual scroll only) ─────────────────────────
  var _currentHits    = [];   // flat hits array for current virtual-scroll result
  var _pendingExpansions = Object.create(null); // relative_path → true
  var _searchEpoch   = 0;    // incremented each search so stale responses are ignored

  function renderDirTree(hits) {
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
      html += '<div class="dir-node"><div class="dir-hdr"><span class="chev">&#9660;</span>';
      html += '<span class="dir-name">' + esc(dir ? dir + '/' : '(root)') + '</span></div>';
      html += '<div class="dir-body">';
      dirs[dir].forEach(function(hit) {
        var doc = hit.document;
        var matches = hit._matches || [];
        var fname = doc.filename || (doc.relative_path || '').split('/').pop() || '';
        html += '<div class="file-node"><div class="file-hdr" tabindex="0" data-path="' + esc(doc.relative_path) + '">';
        html += '<span class="file-name">' + esc(fname) + '</span></div>';
        if (matches.length > 0) {
          html += '<div class="file-body">';
          matches.forEach(function(m, i) {
            var br = (i === matches.length - 1) ? '└─' : '├─';
            var la = m.line != null ? ' data-line="' + m.line + '"' : '';
            html += '<div class="match-item" tabindex="0" data-path="' + esc(doc.relative_path) + '"' + la + '>';
            html += '<span class="tree-branch">' + br + '</span>';
            html += '<span class="match-text">' + esc(m.text) + '</span>';
            if (m.line != null) { html += '<span class="match-line">:' + (m.line + 1) + '</span>'; }
            html += '</div>';
          });
          html += '</div>';
        }
        html += '</div>';
      });
      html += '</div></div>';
    });
    return html;
  }

  function attachCappedDirHandlers() {
    _cappedEl.querySelectorAll('.dir-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() { hdr.parentNode.classList.toggle('collapsed'); });
    });
    _cappedEl.querySelectorAll('.file-hdr').forEach(function(hdr) {
      hdr.addEventListener('click', function() {
        vscode.postMessage({ type: 'openFile', relativePath: hdr.dataset.path,
          root: currentSearch.root, query: currentSearch.query });
      });
      hdr.addEventListener('keydown', function(e) { if (e.key === 'Enter') { hdr.click(); } });
    });
    _cappedEl.querySelectorAll('.match-item').forEach(function(item) {
      item.addEventListener('click', function(ev) {
        ev.stopPropagation();
        var line = (item.dataset.line !== undefined && item.dataset.line !== '')
          ? parseInt(item.dataset.line, 10) : undefined;
        vscode.postMessage({ type: 'openFile', relativePath: item.dataset.path,
          root: currentSearch.root, line: line, query: currentSearch.query });
      });
      item.addEventListener('keydown', function(e) { if (e.key === 'Enter') { item.click(); } });
    });
  }

  function renderCappedTree() {
    var html = '<div class="cap-hint">Too many results — click a folder to expand</div>';
    currentFacets.forEach(function(f) {
      var sub = f.value;
      var exp = subExpansions[sub];
      var isOpen = exp && exp.state !== 'idle';
      html += '<div class="sub-node" id="capped-sub-' + esc(sub) + '">';
      html += '<div class="sub-hdr is-cap" tabindex="0" data-sub="' + esc(sub) + '">';
      html += '<span class="chev" style="' + (isOpen ? '' : 'transform:rotate(-90deg)') + '">&#9660;</span>';
      html += '<span class="sub-name">' + esc(sub) + '</span><span class="badge">' + f.count + '</span>';
      if (exp && exp.state === 'loading') { html += '<span class="cap-loading">Loading…</span>'; }
      html += '</div>';
      if (exp && exp.state === 'loaded') {
        html += '<div class="sub-body">';
        if (exp.hits.length === 0) {
          html += '<div class="cap-still-capped">No results</div>';
        } else {
          if (exp.capped) {
            html += '<div class="cap-still-capped">Showing ' + exp.hits.length + ' of ' + exp.found
                  + ' — narrow further with the Sub filter</div>';
          }
          html += renderDirTree(exp.hits);
        }
        html += '</div>';
      }
      html += '</div>';
    });
    return html;
  }

  function handleSubExpand(sub) {
    var exp = subExpansions[sub];
    if (!exp || exp.state === 'idle') {
      subExpansions[sub] = { state: 'loading' };
      refreshCappedNode(sub);
      vscode.postMessage({ type: 'expandSub', sub: sub, query: _cappedSearch.query,
        mode: _cappedSearch.mode, ext: _cappedSearch.ext, root: _cappedSearch.root });
    } else {
      var node = document.getElementById('capped-sub-' + sub);
      if (node) { node.classList.toggle('collapsed'); }
    }
  }

  function refreshCappedNode(sub) {
    var node = document.getElementById('capped-sub-' + sub);
    if (!node) { return; }
    var exp = subExpansions[sub];
    var isOpen = exp && exp.state !== 'idle';
    var hdr = node.querySelector('.sub-hdr.is-cap');
    if (hdr) {
      var chev = hdr.querySelector('.chev');
      if (chev) { chev.style.transform = isOpen ? '' : 'rotate(-90deg)'; }
      var existing = hdr.querySelector('.cap-loading');
      if (exp && exp.state === 'loading') {
        if (!existing) {
          var s = document.createElement('span');
          s.className = 'cap-loading'; s.textContent = 'Loading…'; hdr.appendChild(s);
        }
      } else if (existing) { existing.remove(); }
    }
    var body = node.querySelector('.sub-body');
    if (exp && exp.state === 'loaded') {
      if (!body) { body = document.createElement('div'); body.className = 'sub-body'; node.appendChild(body); }
      var inner = exp.capped
        ? '<div class="cap-still-capped">Showing ' + exp.hits.length + ' of ' + exp.found
          + ' — narrow further with the Sub filter</div>' : '';
      inner += exp.hits.length === 0 ? '<div class="cap-still-capped">No results</div>' : renderDirTree(exp.hits);
      body.innerHTML = inner;
      attachCappedDirHandlers();
    } else if (body) { body.remove(); }
  }

  // ── Main result display ───────────────────────────────────────────────────

  function showResults(data) {
    hideConfigError();
    var hits  = data.hits  || [];
    var found = data.found || 0;
    var isCapped = found > hits.length && found > 0;
    var modeEntry = MODES.find(function(m) { return m.key === data.mode; });
    var modeLabel = modeEntry ? modeEntry.label : data.mode;
    statusEl.className = 'status-bar';

    if (found === 0) {
      statusEl.textContent = 'No results';
      _setMode('empty');
      _msgEl.innerHTML = 'No results for <strong>' + esc(data.query) + '</strong>';
      return;
    }

    if (isCapped) {
      statusEl.textContent = found + ' files matched — too many to show all — ' + modeLabel + ' mode';
      _cappedSearch = { query: data.query, mode: data.mode, ext: data.ext || '', root: data.root || currentSearch.root };
      currentFacets = [];
      if (data.facet_counts) {
        var sf = data.facet_counts.find(function(f) { return f.field_name === 'path_segments'; });
        if (sf) {
          // Show top-level folders only (no '/' in the segment value)
          currentFacets = (sf.counts || []).filter(function(c) { return c.value && c.value.indexOf('/') < 0; });
        }
      }
      if (currentFacets.length === 0) {
        var seen = {};
        hits.forEach(function(h) {
          var rel = (h.document.relative_path || '').replace(/\\\\/g, '/');
          var i = rel.indexOf('/');
          var s = i > 0 ? rel.slice(0, i) : '';
          seen[s] = (seen[s] || 0) + 1;
        });
        currentFacets = Object.keys(seen).sort().map(function(s) { return { value: s, count: seen[s] }; });
      }
      _setMode('capped');
      _cappedEl.innerHTML = renderCappedTree();
      _cappedEl.querySelectorAll('.sub-hdr.is-cap').forEach(function(hdr) {
        hdr.addEventListener('keydown', function(e) {
          if (e.key === 'Enter' || e.key === ' ') { handleSubExpand(hdr.dataset.sub); e.preventDefault(); }
        });
      });
      return;
    }

    // ── Virtual scroll path ───────────────────────────────────────────────
    statusEl.textContent = found + ' result' + (found === 1 ? '' : 's') + ' — '
      + data.elapsed + 'ms — ' + modeLabel + ' mode';
    _setMode('virtual');
    _collapsedSubs   = Object.create(null);
    _collapsedDirIds = Object.create(null);
    _currentHits = hits;
    _rows = _buildRows(hits);
    _computeVis();
    resultsEl.scrollTop = 0;
    _scheduleRender();
  }

  // ── Search trigger ────────────────────────────────────────────────────────
  var timer = null;
  function triggerSearch() {
    clearTimeout(timer);
    timer = setTimeout(function() {
      var query = qEl.value.trim();
      if (!query) {
        _setMode('empty');
        _msgEl.textContent = 'Type to search across your codebase';
        statusEl.textContent = 'Built: ' + BUILD_DATE; statusEl.className = 'status-bar';
        return;
      }
      subExpansions = {};
      _pendingExpansions = Object.create(null);
      _searchEpoch++;
      statusEl.innerHTML = 'Searching<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
      statusEl.className = 'status-bar';
      var params = { type: 'search', query: query, mode: modeEl.value,
        ext: extEl.value.trim(), sub: subEl.value.trim(), root: rootEl.value, limit: 20 };
      currentSearch = { query: params.query, mode: params.mode, ext: params.ext,
                        sub: params.sub, root: params.root };
      vscode.postMessage(params);
    }, 180);
  }

  qEl.addEventListener('input',   triggerSearch);
  modeEl.addEventListener('change', triggerSearch);
  extEl.addEventListener('input',  triggerSearch);
  subEl.addEventListener('input',  triggerSearch);
  rootEl.addEventListener('change', triggerSearch);

  qEl.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') {
      var f = resultsEl.querySelector('.file-hdr,.match-item');
      if (f) { f.focus(); e.preventDefault(); }
    }
  });

  // ── Message handler ───────────────────────────────────────────────────────
  window.addEventListener('message', function(ev) {
    try {
    var msg = ev.data;
    if (msg.type === 'results') { showResults(msg); }
    else if (msg.type === 'fileExpanded') {
      if (msg.epoch !== _searchEpoch) { return; } // stale response from a previous search
      var hit = null;
      for (var hi = 0; hi < _currentHits.length; hi++) {
        if (_currentHits[hi].document.relative_path === msg.relativePath) { hit = _currentHits[hi]; break; }
      }
      if (hit && (msg.matches.length > 0 || !hit.ast_expanded)) {
        hit._matches = msg.matches || [];
        hit.ast_expanded = true;
        _rows = _buildRows(_currentHits);
        _computeVis();
        _scheduleRender();
      }
    } else if (msg.type === 'subResults') {
      var sub = msg.sub;
      if (msg.error) {
        subExpansions[sub] = { state: 'loaded', hits: [], found: 0, capped: false };
      } else {
        var hits = msg.hits || [];
        var found = msg.found || 0;
        subExpansions[sub] = { state: 'loaded', hits: hits, found: found, capped: found > hits.length };
      }
      refreshCappedNode(sub);
    } else if (msg.type === 'error') {
      statusEl.textContent = 'Error: ' + msg.message;
      statusEl.className = 'status-bar error';
      _setMode('empty');
      _msgEl.innerHTML = 'Search failed — is the codesearch daemon running?<br><small>Run: ts start</small>';
    } else if (msg.type === 'configError') { showConfigError(msg.message); }
    } catch (err) { vscode.postMessage({ type: 'jsError', message: 'message handler: ' + (err instanceof Error ? err.message : String(err)) }); }
  });

  // Initial state
  _setMode('empty');
  statusEl.textContent = 'Built: ' + BUILD_DATE;
  qEl.focus();
})();
</script>
</body>
</html>`;
}
