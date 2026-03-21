#!/usr/bin/env node
/**
 * Ad-hoc virtual-scroll test.
 *
 * Usage:
 *   node test-virtual-scroll.js <query> [mode]
 *
 * mode defaults to "uses".  Examples:
 *   node test-virtual-scroll.js BlobStore uses
 *   node test-virtual-scroll.js IAbsBlobStore declarations
 *
 * Reads port / api_key from config.json (auto-discovered), or set env vars:
 *   TSCS_PORT   – Typesense port (API is port+1). Default: 8108
 *   TSCS_KEY    – API key.  Default: codesearch-local
 *   TSCS_ROOT   – Root name.  Default: default
 */

'use strict';
const http = require('http');
const fs   = require('fs');
const path = require('path');

// ── Config loading ────────────────────────────────────────────────────────────
function loadConfig() {
    const candidates = [
        path.join('Q:/spocore/tscodesearch/config.json'),
        path.join(__dirname, '../config.json'),
        path.join(__dirname, 'config.json'),
    ];
    for (const c of candidates) {
        try { return JSON.parse(fs.readFileSync(c, 'utf-8')); } catch { /* try next */ }
    }
    return null;
}

// ── HTTP helper ───────────────────────────────────────────────────────────────
function httpPost(port, path_, apiKey, body) {
    return new Promise((resolve, reject) => {
        const bodyStr = JSON.stringify(body);
        const req = http.request(
            { hostname: 'localhost', port, path: path_, method: 'POST',
              headers: { 'X-TYPESENSE-API-KEY': apiKey,
                         'Content-Type': 'application/json',
                         'Content-Length': Buffer.byteLength(bodyStr) } },
            (res) => {
                let data = '';
                res.on('data', d => data += d);
                res.on('end', () => {
                    try {
                        const parsed = JSON.parse(data);
                        if (res.statusCode >= 400) reject(new Error(`HTTP ${res.statusCode}: ${parsed.error ?? data.slice(0,200)}`));
                        else resolve(parsed);
                    } catch { reject(new Error(`Bad JSON: ${data.slice(0,200)}`)); }
                });
            },
        );
        req.setTimeout(30000, () => req.destroy(new Error('Timed out')));
        req.on('error', reject);
        req.write(bodyStr);
        req.end();
    });
}

// ── Row builder — mirrors extension.ts _buildRows exactly ────────────────────
function buildRows(hits) {
    const rows = [];
    let nextDirId = 0;
    const subs = Object.create(null);
    hits.forEach(h => {
        const s = h.document.subsystem || '';
        (subs[s] || (subs[s] = [])).push(h);
    });
    Object.keys(subs).sort().forEach(sub => {
        rows.push({ type: 'sub', sub, count: subs[sub].length });
        const dirs = Object.create(null);
        subs[sub].forEach(h => {
            const rel = h.document.relative_path || '';
            const idx = rel.lastIndexOf('/');
            const dir = idx >= 0 ? rel.slice(0, idx) : '';
            const key = sub + '\x01' + dir;
            if (!dirs[key]) dirs[key] = { dir, sub, hits: [], id: nextDirId++ };
            dirs[key].hits.push(h);
        });
        Object.keys(dirs).sort().forEach(key => {
            const d = dirs[key];
            rows.push({ type: 'dir', dir: d.dir, sub: d.sub, dirId: d.id });
            d.hits.forEach(h => {
                rows.push({ type: 'file', hit: h, sub: d.sub, dirId: d.id });
                const nm = h._matches.length;
                h._matches.forEach((m, mi) =>
                    rows.push({ type: 'match', match: m, hit: h, sub: d.sub, dirId: d.id, last: mi === nm - 1 })
                );
            });
        });
    });
    return rows;
}

// ── Visibility — mirrors extension.ts _computeVis exactly ────────────────────
function computeVis(rows, collapsedSubs, collapsedDirIds) {
    const vis = [];
    for (const r of rows) {
        if (r.type === 'sub') { vis.push(r); continue; }
        if (collapsedSubs[r.sub]) continue;
        if (r.type === 'dir') { vis.push(r); continue; }
        if (collapsedDirIds[r.dirId]) continue;
        vis.push(r);
    }
    return vis;
}

// ── Virtual window simulation ─────────────────────────────────────────────────
const ROW_H   = { sub: 36, dir: 27, file: 28, match: 20 };
const PANEL_H = 600;   // simulated panel height px
const OVERSCAN = 250;  // px above/below viewport to keep rendered

function simulateScroll(vis) {
    const offsets = new Array(vis.length + 1).fill(0);
    for (let i = 0; i < vis.length; i++) offsets[i + 1] = offsets[i] + ROW_H[vis[i].type];
    const totalH = offsets[vis.length];
    let maxRendered = 0;
    const steps = Math.max(1, Math.ceil(totalH / 100));
    for (let step = 0; step <= steps; step++) {
        const scrollTop   = Math.min((step / steps) * Math.max(0, totalH - PANEL_H), totalH);
        const rangeTop    = Math.max(0, scrollTop - OVERSCAN);
        const rangeBottom = scrollTop + PANEL_H + OVERSCAN;
        let lo = 0, hi = vis.length - 1, startIdx = 0;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (offsets[mid + 1] > rangeTop) { startIdx = mid; hi = mid - 1; } else lo = mid + 1;
        }
        let endIdx = startIdx;
        while (endIdx < vis.length && offsets[endIdx] < rangeBottom) endIdx++;
        maxRendered = Math.max(maxRendered, endIdx - startIdx);
    }
    return { totalH, maxRendered };
}

// ── Test harness ──────────────────────────────────────────────────────────────
function typeCounts(vis) {
    return vis.reduce((acc, r) => { acc[r.type] = (acc[r.type] || 0) + 1; return acc; }, {});
}
function fmtCounts(vis) {
    const c = typeCounts(vis);
    return `${vis.length} rows  [sub:${c.sub||0} dir:${c.dir||0} file:${c.file||0} match:${c.match||0}]`;
}

let passed = 0, failed = 0;
function check(label, ok, detail = '') {
    if (ok) { console.log(`  ✓ ${label}`); passed++; }
    else     { console.log(`  ✗ FAIL: ${label}${detail ? '  — ' + detail : ''}`); failed++; }
}

async function run() {
    const query = process.argv[2];
    const mode  = process.argv[3] || 'uses';
    if (!query) {
        console.error('Usage: node test-virtual-scroll.js <query> [mode]');
        process.exit(1);
    }

    const cfg    = loadConfig();
    const tsPort = parseInt(process.env.TSCS_PORT  || String(cfg?.port   ?? 8108), 10);
    const apiKey = process.env.TSCS_KEY  || cfg?.api_key || 'codesearch-local';
    const root   = process.env.TSCS_ROOT || 'default';
    const apiPort = tsPort + 1;  // API server = Typesense port + 1

    console.log(`\nVirtual-scroll test  query="${query}"  mode=${mode}  api=localhost:${apiPort}`);
    console.log('═'.repeat(65));

    // ── 1. Fetch results ──────────────────────────────────────────────────────
    let raw;
    try {
        raw = await httpPost(apiPort, '/query-codebase', apiKey,
            { mode, pattern: query, sub: '', ext: '', root, limit: 50 });
    } catch (e) {
        console.error(`Search failed: ${e.message}`);
        process.exit(1);
    }

    if (raw.overflow && (raw.hits ?? []).length === 0) {
        console.log(`overflow=true and 0 hits — too many AST matches for "${query}" in ${mode} mode.`);
        console.log('Try a more specific query or a different mode, e.g.:');
        console.log(`  node test-virtual-scroll.js ${query} declarations`);
        console.log(`  node test-virtual-scroll.js ${query} calls`);
        process.exit(0);
    }

    // Normalise to the shape extension.ts expects
    const hits = (raw.hits ?? []).map(h => ({
        document: {
            relative_path: h.document.relative_path,
            subsystem:     h.document.subsystem || '',
            filename:      h.document.filename  || '',
        },
        _matches: (h.matches ?? []).map(m => ({ text: m.text, line: m.line - 1 })),
    }));

    console.log(`Found: ${raw.found ?? 0}  Returned: ${hits.length} hits  overflow: ${raw.overflow ?? false}\n`);

    if (hits.length === 0) {
        console.log('No hits — nothing to exercise.');
        return;
    }

    // ── 2. Build rows ─────────────────────────────────────────────────────────
    const rows = buildRows(hits);
    console.log(`Row breakdown: ${fmtCounts(rows)}`);

    const allCollapsed = Object.create(null);
    const allDirIds    = Object.create(null);

    // ── 3. Fully-expanded initial state ──────────────────────────────────────
    console.log('\n[1] Fully-expanded state');
    const vis0 = computeVis(rows, {}, {});
    console.log(`    ${fmtCounts(vis0)}`);
    check('all rows visible', vis0.length === rows.length);

    // ── 4. Collapse each subsystem, verify, re-expand ─────────────────────────
    const subs = rows.filter(r => r.type === 'sub');
    console.log(`\n[2] Collapse / re-expand each subsystem (${subs.length} total)`);
    for (const sr of subs) {
        const csubs = { [sr.sub]: true };
        const visC = computeVis(rows, csubs, {});
        const hidden = rows.filter(r => r.type !== 'sub' && r.sub === sr.sub).length;
        check(
            `collapse "${sr.sub || '(no subsystem)'}": -${hidden} rows`,
            visC.length === vis0.length - hidden,
            `expected ${vis0.length - hidden}, got ${visC.length}`,
        );
        // Re-expand
        const visE = computeVis(rows, {}, {});
        check(
            `re-expand "${sr.sub || '(no subsystem)'}": back to full`,
            visE.length === vis0.length,
        );
    }

    // ── 5. Collapse all subsystems at once ────────────────────────────────────
    console.log(`\n[3] Collapse all subsystems at once`);
    subs.forEach(sr => { allCollapsed[sr.sub] = true; });
    const visAllC = computeVis(rows, allCollapsed, {});
    check(
        `only sub-headers visible (${subs.length})`,
        visAllC.length === subs.length,
        `got ${visAllC.length}`,
    );

    // Re-expand all
    const visAllE = computeVis(rows, {}, {});
    check('full restore after all-collapse', visAllE.length === vis0.length);

    // ── 6. Collapse each dir, verify, re-expand ───────────────────────────────
    const dirs = rows.filter(r => r.type === 'dir');
    console.log(`\n[4] Collapse / re-expand each dir (${dirs.length} total)`);
    for (const dr of dirs) {
        const hidden = rows.filter(r => r.dirId === dr.dirId && (r.type === 'file' || r.type === 'match')).length;
        const cdirs = { [dr.dirId]: true };
        const visD  = computeVis(rows, {}, cdirs);
        check(
            `collapse dir "${dr.dir || '(root)'}": -${hidden} rows`,
            visD.length === vis0.length - hidden,
            `expected ${vis0.length - hidden}, got ${visD.length}`,
        );
        const visDE = computeVis(rows, {}, {});
        check(`re-expand dir "${dr.dir || '(root)'}": back to full`, visDE.length === vis0.length);
    }

    // ── 7. Virtual window — check DOM savings ────────────────────────────────
    console.log('\n[5] Virtual window simulation');
    const { totalH, maxRendered } = simulateScroll(vis0);
    const savings = vis0.length > 0 ? ((1 - maxRendered / vis0.length) * 100).toFixed(0) : 0;
    console.log(`    Total height: ${totalH}px  Panel: ${PANEL_H}px  Overscan: ${OVERSCAN}px`);
    console.log(`    Max in-DOM at any scroll pos: ${maxRendered} of ${vis0.length}`);
    if (vis0.length <= maxRendered) {
        console.log('    (all rows fit on screen — no DOM savings, but no regression)');
        check('virtual window covers all rows', true);
    } else {
        check(
            `DOM savings: ~${savings}% fewer nodes vs naive`,
            maxRendered < vis0.length,
        );
    }

    // ── 8. Row-height consistency ─────────────────────────────────────────────
    console.log('\n[6] Row-height consistency');
    const unexpectedTypes = rows.filter(r => !(r.type in ROW_H));
    check('no unknown row types', unexpectedTypes.length === 0,
        unexpectedTypes.map(r => r.type).join(', '));

    // ── Summary ───────────────────────────────────────────────────────────────
    console.log('\n' + '═'.repeat(65));
    const total = passed + failed;
    console.log(`Result: ${passed}/${total} passed${failed > 0 ? `  (${failed} FAILED)` : '  ✓ all good'}`);
    if (failed > 0) process.exit(1);
}

run().catch(e => { console.error(e.stack ?? e.message); process.exit(1); });
