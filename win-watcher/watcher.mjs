/**
 * Windows filesystem watcher for codesearch.
 *
 * Watches Windows-path source roots (e.g. C:/myproject/src) using native
 * ReadDirectoryChangesW events via chokidar, then sends batched file-change
 * notifications to the indexserver management API (POST /file-events).
 *
 * Only roots with Windows-style drive paths (X:/) are watched here.
 * Native Linux paths are handled by the WSL watcher (watcher.py).
 *
 * Usage:
 *   cd win-watcher
 *   npm install
 *   node watcher.mjs
 */

import chokidar from 'chokidar';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

// ── Config ────────────────────────────────────────────────────────────────────

const CONFIG_PATH = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    '..', 'config.json'
);

let config;
try {
    config = JSON.parse(readFileSync(CONFIG_PATH, 'utf8'));
} catch (e) {
    console.error(`[watcher] ERROR: Cannot read config.json at ${CONFIG_PATH}: ${e.message}`);
    process.exit(1);
}

process.title = 'codesearch win-watcher';

const API_KEY   = config.api_key;
const PORT      = (config.port || 8108) + 1;   // management API = Typesense port + 1
const RAW_ROOTS = config.roots ?? { default: config.src_root };

// Only watch roots that have Windows-style drive paths (C:/ Q:/ etc.)
const WIN_PATH_RE = /^[A-Za-z]:[/\\]/;
const roots = Object.entries(RAW_ROOTS).filter(([, p]) => WIN_PATH_RE.test(p));

if (roots.length === 0) {
    console.log('[watcher] No Windows-path roots found in config.json — nothing to watch.');
    process.exit(0);
}

const DEBOUNCE_MS = 2000;
const API_BASE    = `http://localhost:${PORT}`;
const API_URL     = `${API_BASE}/file-events`;

// ── Management API helpers ────────────────────────────────────────────────────

async function apiPost(path) {
    const resp = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'X-TYPESENSE-API-KEY': API_KEY },
    });
    return resp.json();
}

async function pauseServerWatcher() {
    try {
        const r = await apiPost('/watcher/pause');
        console.log(`[watcher] Server-side polling watcher paused (${JSON.stringify(r)})`);
    } catch (e) {
        console.warn(`[watcher] Could not pause server watcher: ${e.message}`);
    }
}

async function resumeServerWatcher() {
    try {
        const r = await apiPost('/watcher/resume');
        console.log(`[watcher] Server-side polling watcher resumed (${JSON.stringify(r)})`);
    } catch (e) {
        console.warn(`[watcher] Could not resume server watcher: ${e.message}`);
    }
}

// Mirror EXCLUDE_DIRS from indexserver/config.py
const EXCLUDED_DIRS_RE = /(^|[/\\])(\.git|obj|bin|node_modules|\.venv|__pycache__|\.vs|Target|Build|Import|nugetcache|target|debug|ship|x64|x86)([/\\]|$)/;

// ── Event queue + debounced flush ─────────────────────────────────────────────

/** @type {Map<string, 'upsert'|'delete'>} */
const pending = new Map();
let flushTimer = null;
let inFlight   = false;   // true while an HTTP request to the API is in progress

function scheduleFlush() {
    // If a flush is already in progress, don't start a timer — the completion
    // handler will drain pending immediately when the current batch finishes.
    if (inFlight) return;
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, DEBOUNCE_MS);
}

async function flush() {
    if (inFlight || pending.size === 0) return;
    inFlight   = true;
    flushTimer = null;

    const events = Array.from(pending.entries()).map(([p, action]) => ({ path: p, action }));
    pending.clear();

    const ts = () => new Date().toISOString().replace('T', ' ').slice(0, 19);
    console.log(`[watcher] ${ts()}  sending ${events.length} event(s)${pending.size > 0 ? `  (${pending.size} more queued)` : ''}`);

    try {
        const resp = await fetch(API_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-TYPESENSE-API-KEY': API_KEY,
            },
            body: JSON.stringify({ events }),
        });

        if (!resp.ok) {
            const text = await resp.text();
            console.error(`[watcher] API error ${resp.status}: ${text}`);
            // Re-queue on server error so nothing is lost
            for (const ev of events) pending.set(ev.path, ev.action);
        } else {
            const result = await resp.json();
            console.log(`[watcher] ${ts()}  queued=${result.queued ?? '?'}  deduped=${result.deduped ?? '?'}`);
        }
    } catch (e) {
        console.error(`[watcher] flush error: ${e.message}`);
        // Re-queue on network error
        for (const ev of events) pending.set(ev.path, ev.action);
    }

    inFlight = false;

    // Drain any events that arrived while we were sending
    if (pending.size > 0) {
        console.log(`[watcher] ${ts()}  ${pending.size} queued event(s) — flushing next batch`);
        await flush();
    }
}

// ── Start watchers ────────────────────────────────────────────────────────────

for (const [name, root] of roots) {
    const watchRoot = root.replace(/\\/g, '/');
    console.log(`[watcher] Watching ${watchRoot}  (root: ${name})`);

    const watcher = chokidar.watch(watchRoot, {
        persistent:       true,
        ignoreInitial:    true,
        usePolling:       false,    // native ReadDirectoryChangesW — no polling
        awaitWriteFinish: {
            stabilityThreshold: 500,    // wait 500 ms of silence before firing
            pollInterval:       100,
        },
        ignored: EXCLUDED_DIRS_RE,
    });

    watcher
        .on('add',    p => { pending.set(p.replace(/\\/g, '/'), 'upsert'); scheduleFlush(); })
        .on('change', p => { pending.set(p.replace(/\\/g, '/'), 'upsert'); scheduleFlush(); })
        .on('unlink', p => { pending.set(p.replace(/\\/g, '/'), 'delete'); scheduleFlush(); })
        .on('error',  e => console.error(`[watcher] fs error: ${e}`));
}

// ── Startup: pause the WSL PollingObserver (we handle events natively) ────────

console.log(`[watcher] Sending events to ${API_URL}`);
await pauseServerWatcher();
console.log('[watcher] Ready — waiting for file changes...');

// ── Shutdown: resume the WSL PollingObserver before exiting ──────────────────

async function shutdown(signal) {
    console.log(`\n[watcher] ${signal} received — resuming server watcher and exiting...`);
    await resumeServerWatcher();
    process.exit(0);
}

process.on('SIGINT',  () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
