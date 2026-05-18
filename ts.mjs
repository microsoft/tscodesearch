#!/usr/bin/env node
/**
 * ts.mjs -- codesearch management CLI
 *
 * The daemon is a single Python process (.client-venv) that owns the local
 * Tantivy index. There is no longer a Typesense server, WSL bridge, or
 * Docker container to manage -- start/stop manage the daemon itself.
 *
 * Usage: ts <command> [options]
 *
 * Commands:
 *   start                  Start the daemon
 *   stop                   Stop the daemon
 *   restart                Stop then start the daemon
 *   status                 Show daemon health and index statistics
 *   verify [--root NAME]   Scan filesystem and repair stale/missing entries
 *          [--no-delete-orphans]
 *   recreate [--root NAME] Stop daemon, wipe index dir, restart (full reindex)
 *   log [-n N] [-f]        Show daemon log
 *   root                   List configured roots
 *   root --add NAME PATH   Add (or update) a root in config.json
 *   root --remove NAME     Remove a root from config.json
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync, spawn } from 'child_process';
import http from 'http';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// -- Config --------------------------------------------------------------------

function readConfig() {
    const f = path.join(__dirname, 'config.json');
    try {
        return JSON.parse(fs.readFileSync(f, 'utf-8'));
    } catch (e) {
        die(`Cannot read config.json: ${e.message}`);
    }
}

function saveConfig(updated) {
    const f = path.join(__dirname, 'config.json');
    fs.writeFileSync(f, JSON.stringify(updated, null, 2) + '\n', 'utf-8');
}

const cfg     = readConfig();
const API_KEY = cfg.api_key ?? 'codesearch-local';
const PORT    = cfg.port    ?? 8108;
const ROOTS   = cfg.roots   ?? {};

// -- Helpers -------------------------------------------------------------------

function log(msg)  { console.log(`[ts] ${msg}`); }
function die(msg)  { console.error(`[ts] ERROR: ${msg}`); process.exit(1); }

// -- HTTP helpers --------------------------------------------------------------

function apiGet(urlPath, timeoutMs = 5000) {
    return new Promise((resolve, reject) => {
        const req = http.request({
            host: 'localhost', port: PORT, path: urlPath, method: 'GET',
            headers: { 'X-API-KEY': API_KEY },
        }, res => {
            let data = '';
            res.on('data', d => data += d);
            res.on('end', () => {
                try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
                catch { resolve({ status: res.statusCode, body: data }); }
            });
        });
        req.on('error', reject);
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
        req.end();
    });
}

function apiPost(urlPath, body, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = http.request({
            host: 'localhost', port: PORT, path: urlPath, method: 'POST',
            headers: {
                'X-API-KEY': API_KEY,
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(data),
            },
        }, res => {
            let out = '';
            res.on('data', d => out += d);
            res.on('end', () => {
                try { resolve({ status: res.statusCode, body: JSON.parse(out) }); }
                catch { resolve({ status: res.statusCode, body: out }); }
            });
        });
        req.on('error', reject);
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
        req.write(data);
        req.end();
    });
}

function clientVenvPython() {
    return path.join(__dirname, '.client-venv', 'Scripts', 'python.exe');
}

/** Returns tscodesearch.exe if present (descriptive process name in Task Manager),
 *  otherwise falls back to python.exe. Used only for daemon spawn. */
function daemonPython() {
    const named = path.join(__dirname, '.client-venv', 'Scripts', 'tscodesearch.exe');
    return fs.existsSync(named) ? named : clientVenvPython();
}

// Mirrors indexserver.config.collection_for_root: lowercase, then [^a-z0-9_] -> _.
function collectionForRoot(name) {
    return `codesearch_${name.toLowerCase().replace(/[^a-z0-9_]/g, '_')}`;
}

function indexDirForRoot(name) {
    return path.join(__dirname, '.tantivy', collectionForRoot(name));
}

async function pollHealth(port, timeoutMs = 60_000, label = 'daemon') {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const result = await new Promise((resolve, reject) => {
                const req = http.request(
                    { host: 'localhost', port, path: '/health', method: 'GET' },
                    res => { res.resume(); resolve(res.statusCode); }
                );
                req.on('error', reject);
                req.setTimeout(2000, () => { req.destroy(); reject(new Error('timeout')); });
                req.end();
            });
            if (result === 200) return;
        } catch { /* not up yet */ }
        await new Promise(r => setTimeout(r, 500));
    }
    die(`${label} did not become healthy within ${timeoutMs / 1000}s`);
}

/** Poll until the port stops responding. Returns true if it closed within the
 *  timeout, false if the timeout elapsed and the port is still up. */
async function waitForPortClosed(port, timeoutMs = 10_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const still_up = await new Promise(resolve => {
            const req = http.request(
                { host: 'localhost', port, path: '/health', method: 'GET' },
                res => { res.resume(); resolve(true); }
            );
            // error = connection refused/reset = port is gone
            req.on('error', () => resolve(false));
            // timeout = server slow (GIL contention) = still up; don't false-positive
            req.setTimeout(2000, () => { req.destroy(); resolve(true); });
            req.end();
        });
        if (!still_up) return true;
        await new Promise(r => setTimeout(r, 200));
    }
    return false;  // timed out -- port still up
}

async function shutdownDaemon() {
    const pidFile = path.join(
        process.env.LOCALAPPDATA ?? path.join(process.env.USERPROFILE ?? '', 'AppData', 'Local'),
        'tscodesearch', 'daemon.pid'
    );
    let daemonPid = null;
    try { daemonPid = fs.readFileSync(pidFile, 'utf-8').trim() || null; } catch { /* no pid file */ }

    try {
        await apiPost('/management/shutdown', {}, 5000);
        log('Shutdown sent.');
    } catch {
        log('Daemon not reachable (already stopped?).');
        return;
    }

    // The daemon's stop_daemon() does up to 5s syncer join + 5s queue stop +
    // backend close (waits for merge threads) + 5s server shutdown -- so allow
    // up to 30s before assuming it's stuck.
    const closed = await waitForPortClosed(PORT, 30_000);

    if (!closed && daemonPid) {
        log(`Daemon did not exit after 30s -- force-killing pid ${daemonPid}...`);
        if (process.platform === 'win32') {
            spawnSync('taskkill', ['/F', '/PID', daemonPid], { stdio: 'pipe' });
        } else {
            spawnSync('kill', ['-9', daemonPid], { stdio: 'pipe' });
        }
        await waitForPortClosed(PORT, 5_000);
    }
}

function daemonLogFile() {
    const base = process.env.LOCALAPPDATA
        ?? path.join(process.env.USERPROFILE ?? '', 'AppData', 'Local');
    return path.join(base, 'tscodesearch', 'daemon.log');
}

function startDaemon() {
    const py = daemonPython();
    if (!fs.existsSync(py)) {
        die(`.client-venv not found at ${py} -- run setup.cmd first`);
    }
    const logPath = daemonLogFile();
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    const logFd = fs.openSync(logPath, 'a');
    const child = spawn(py, [path.join(__dirname, 'tsquery_server.py')], {
        detached: true,
        stdio: ['ignore', logFd, logFd],
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
    child.unref();
    fs.closeSync(logFd);
    log(`Daemon started (detached). Log: ${logPath}`);
}

// -- Status display ------------------------------------------------------------

function fmtNum(n)  { return n == null ? '?' : Number(n).toLocaleString(); }
function fmtTs(ts)  { return ts ? ts.replace('T', ' ').substring(0, 16) : ''; }

function printStatus(apiBody) {
    const collections = apiBody?.collections ?? {};
    const syncer      = apiBody?.syncer      ?? {};
    const watcher     = apiBody?.watcher     ?? {};
    const queue       = apiBody?.queue       ?? {};
    const prog        = syncer.progress      ?? {};
    const syncerRunning = syncer.running ?? false;

    const qDepth    = queue.depth    ?? 0;
    const qEnqueued = queue.enqueued ?? 0;
    const qUpserted = queue.upserted ?? 0;
    const qDeduped  = queue.deduped  ?? 0;
    const qDeleted  = queue.deleted  ?? 0;
    const qErrors   = queue.errors   ?? 0;
    const isQueued  = prog.status === 'queued' && qDepth > 0;

    for (const [rootName, info] of Object.entries(collections)) {
        const ndocs    = info?.num_documents;
        const buffered = info?.buffered ?? 0;
        const exists   = info?.collection_exists;
        const synced   = info?.synced;
        const syncedAt = info?.synced_at;
        const isCurrent = prog.collection && prog.collection === info?.collection;
        const buffStr = buffered > 0 ? `  +${fmtNum(buffered)} buffered` : '';

        let badge, detail;
        if (!exists || ndocs == null) {
            badge  = '[--]';
            detail = 'not yet indexed -- run: ts verify';
        } else if (isCurrent && (syncerRunning || isQueued)) {
            const total = prog.total_to_update ?? 0;
            const done  = total > 0 ? Math.max(0, total - qDepth) : qUpserted;
            const pct   = total > 0 ? ` ${Math.floor(done * 100 / total)}%` : '';
            badge  = '[>>]';
            detail = `${fmtNum(ndocs)} docs${buffStr}  indexing  ${fmtNum(done)}/${fmtNum(total)}${pct}`;
        } else if (!synced) {
            badge  = '[~~]';
            detail = `${fmtNum(ndocs)} docs${buffStr}  incomplete sync -- run: ts verify --root ${rootName}`;
        } else {
            const when = syncedAt ? `  synced ${fmtTs(syncedAt)}` : '';
            badge  = '[OK]';
            detail = `${fmtNum(ndocs)} docs${buffStr}${when}`;
        }
        console.log(`  [${rootName}] Index  : ${badge} ${detail}`);
    }

    if (syncerRunning || isQueued || (syncer.pending ?? 0) > 0) {
        const phase     = prog.phase    ?? 'starting';
        const fsFiles   = prog.fs_files ?? 0;
        const idxDocs   = prog.index_docs ?? 0;
        const missing   = prog.missing  ?? 0;
        const stale     = prog.stale    ?? 0;
        const orphaned  = prog.orphaned ?? 0;
        const total     = prog.total_to_update ?? 0;
        const done      = total > 0 ? Math.max(0, total - qDepth) : qUpserted;
        const deleted   = qDeleted + (prog.deleted ?? 0);
        const errors    = qErrors;
        const startedAt  = prog.started_at ?? '';
        const lastUpdate = prog.last_update ?? '';
        const pending   = syncer.pending ?? 0;

        const statusLine = isQueued && !syncerRunning ? `[>>] indexing` : `[>>] ${phase}`;
        console.log(`  Syncer  : ${statusLine}`);
        if (startedAt) console.log(`             started ${fmtTs(startedAt)}${lastUpdate ? `  last update ${fmtTs(lastUpdate)}` : ''}`);
        if (fsFiles > 0 || idxDocs > 0)
            console.log(`             fs=${fmtNum(fsFiles)}  prev_indexed=${fmtNum(idxDocs)}  missing=${fmtNum(missing)}  stale=${fmtNum(stale)}  orphaned=${fmtNum(orphaned)}`);
        if (total > 0 || done > 0) {
            const pct = total > 0 ? ` (${Math.floor(done * 100 / total)}%)` : '';
            console.log(`             written=${fmtNum(done)}/${fmtNum(total)}${pct}  deleted=${fmtNum(deleted)}  errors=${errors}`);
        }
        if (pending > 0) console.log(`             ${pending} more root(s) queued`);
    } else if (prog.status === 'complete') {
        const when   = prog.last_update ? `  completed ${fmtTs(prog.last_update)}` : '';
        const errors = qErrors;
        console.log(`  Syncer  : [OK] last sync complete${when}  upserted=${fmtNum(qUpserted)}  errors=${errors}`);
    }

    if (qEnqueued > 0 || qDepth > 0) {
        const errStr = qErrors > 0 ? `  errors=${qErrors}` : '';
        const throttle = queue.throttle_s > 0 ? `  throttle=${queue.throttle_s.toFixed(1)}s` : '';
        console.log(`  Queue   : depth=${fmtNum(qDepth)}  enqueued=${fmtNum(qEnqueued)}  upserted=${fmtNum(qUpserted)}  deduped=${fmtNum(qDeduped)}  deleted=${fmtNum(qDeleted)}${errStr}${throttle}`);
    }

    const state  = watcher.state ?? (watcher.running ? 'watching' : 'stopped');
    const watchQD = watcher.queue_depth ?? 0;
    if (state === 'watching') {
        console.log(`  Watcher : [OK] watching`);
    } else if (state === 'paused') {
        console.log(`  Watcher : [OK] paused`);
    } else if (state === 'processing') {
        console.log(`  Watcher : [>>] processing  queue_depth=${watchQD}`);
    } else {
        console.log(`  Watcher : [--] stopped`);
    }
}

// -- Commands ------------------------------------------------------------------

async function cmdStart() {
    // Idempotent -- if already up, just report.
    try {
        const { status } = await apiGet('/health', 1500);
        if (status === 200) {
            log('Daemon already running.');
            await cmdStatus();
            return;
        }
    } catch { /* not running */ }
    startDaemon();
    log(`Waiting for daemon on port ${PORT}...`);
    await pollHealth(PORT, 30_000, 'daemon');
    log('Daemon is up. Indexing may still be in progress -- use \'ts status\' to monitor.');
}

async function cmdStop() {
    await shutdownDaemon();
}

async function cmdRestart() {
    await shutdownDaemon();
    startDaemon();
    log(`Waiting for daemon on port ${PORT}...`);
    await pollHealth(PORT, 30_000, 'daemon');
    log('Daemon restarted.');
}

async function cmdStatus() {
    console.log(`-- Codesearch Status -------------------------------------------------`);
    try {
        const { status, body } = await apiGet('/status');
        if (status === 200 && typeof body === 'object') {
            printStatus(body);
        } else {
            console.log('  Daemon: not responding');
        }
    } catch {
        console.log('  Daemon: not responding (start with: ts start)');
    }
    console.log(`----------------------------------------------------------------------`);
}

async function cmdRecreate(args) {
    const rootName = args.root || Object.keys(ROOTS)[0] || 'default';
    const indexDir = indexDirForRoot(rootName);

    log(`Recreate for root '${rootName}': stopping daemon, wiping ${indexDir}, restarting.`);
    await shutdownDaemon();
    if (fs.existsSync(indexDir)) {
        fs.rmSync(indexDir, { recursive: true, force: true });
        log(`Removed ${indexDir}`);
    } else {
        log(`Index directory did not exist: ${indexDir}`);
    }
    startDaemon();
    log(`Waiting for daemon on port ${PORT}...`);
    await pollHealth(PORT, 30_000, 'daemon');
    log(`Daemon restarted. Full reindex in progress -- monitor with: ts status`);
}

async function cmdVerify(args) {
    const rootName = args.root || Object.keys(ROOTS)[0] || 'default';
    try {
        const { status, body } = await apiPost('/verify/start', {
            root: rootName, delete_orphans: !args.noDeleteOrphans,
        });
        if (status === 409) { log('Verifier already running. Monitor with: ts status'); return; }
        if (status !== 200) die(`daemon returned ${status}: ${body?.error ?? JSON.stringify(body)}`);
        log(`Verification started for root '${rootName}'. Monitor with: ts status`);
    } catch (e) {
        die(`Cannot reach daemon: ${e.message}`);
    }
}

function cmdLog(args) {
    const n = args.lines ?? 40;
    const logPath = daemonLogFile();
    if (!fs.existsSync(logPath)) {
        log('Daemon log not found -- start it with: ts start');
        return;
    }
    const lines = fs.readFileSync(logPath, 'utf-8').split('\n');
    console.log(`=== daemon log (${logPath}) ===`);
    console.log(lines.slice(-n).join('\n'));
}

function cmdRoot(args) {
    const current = readConfig();
    const roots   = current.roots ?? {};

    if (args.addName) {
        if (!args.addPath) die('--add requires NAME and PATH');
        const p = args.addPath.replace(/\\/g, '/').replace(/\/+$/, '');
        const existing = (roots[args.addName] && typeof roots[args.addName] === 'object')
            ? roots[args.addName] : {};
        const entry = { ...existing, path: p };
        if (args.extensions !== null) {
            if (args.extensions.length === 0) {
                delete entry.extensions;
            } else {
                entry.extensions = args.extensions;
            }
        }
        roots[args.addName] = entry;
        current.roots = roots;
        saveConfig(current);
        log(`Root '${args.addName}' = ${p}`);
        if (entry.extensions) log(`  extensions = ${entry.extensions.join(',')}`);
        log('Restart the daemon for the change to take effect: ts restart');
        return;
    }

    if (args.removeName) {
        if (!(args.removeName in roots)) die(`Root '${args.removeName}' not found`);
        delete roots[args.removeName];
        current.roots = roots;
        saveConfig(current);
        log(`Root '${args.removeName}' removed.`);
        log('Restart the daemon for the change to take effect: ts restart');
        return;
    }

    const names = Object.keys(roots);
    if (!names.length) {
        console.log('No roots configured.');
        return;
    }
    console.log('Configured roots:');
    for (const [name, entry] of Object.entries(roots)) {
        const p = (entry && typeof entry === 'object') ? (entry.path ?? JSON.stringify(entry)) : entry;
        const exts = (entry && entry.extensions && entry.extensions.length)
            ? `  [extensions: ${entry.extensions.join(',')}]` : '';
        console.log(`  ${name.padEnd(16)} ${p}${exts}`);
    }
}

// -- Argument parsing ----------------------------------------------------------

function usage() {
    console.log(`
Usage: ts <command> [options]

Commands:
  start                  Start the daemon
  stop                   Stop the daemon
  restart                Stop then start
  status                 Show daemon health and index stats
  verify                 Scan filesystem and repair index
    --root NAME          Root to verify (default: first configured root)
    --no-delete-orphans  Keep entries for deleted files
  recreate               Stop daemon, wipe index dir, restart (full reindex)
    --root NAME          Root to recreate (default: first configured root)
  log                    Show daemon log
    -n N                 Number of lines (default: 40)
  root                   List configured roots
  root --add NAME PATH   Add (or update) a root in config.json
    --extensions EXTS  Comma-separated extensions to index (e.g. .cs,.py,.ts)
  root --remove NAME     Remove a root from config.json
`.trim());
    process.exit(0);
}

function parseArgs(argv) {
    const [cmd, ...rest] = argv;
    const args = {
        cmd, root: null, noDeleteOrphans: false,
        lines: 40, follow: false,
        addName: null, addPath: null, removeName: null,
        extensions: null,
    };
    for (let i = 0; i < rest.length; i++) {
        switch (rest[i]) {
            case '--root':                args.root = rest[++i]; break;
            case '--no-delete-orphans':   args.noDeleteOrphans = true; break;
            case '-f': case '--follow':   args.follow = true; break;
            case '-n': case '--lines':    args.lines = parseInt(rest[++i], 10) || 40; break;
            case '--add':
                args.addName = rest[++i];
                args.addPath = rest[++i];
                break;
            case '--remove':              args.removeName = rest[++i]; break;
            case '--extensions': {
                const raw = rest[++i] ?? '';
                args.extensions = raw
                    ? raw.split(',').map(e => {
                        e = e.trim();
                        return e.startsWith('.') ? e.toLowerCase() : `.${e.toLowerCase()}`;
                    }).filter(Boolean)
                    : [];
                break;
            }
            default:
                if (rest[i].startsWith('-')) {
                    console.error(`Unknown option: ${rest[i]}`);
                    process.exit(1);
                }
        }
    }
    return args;
}

// -- Main ----------------------------------------------------------------------

const rawArgs = process.argv.slice(2);
if (!rawArgs.length || rawArgs[0] === '--help' || rawArgs[0] === '-h') usage();

const args = parseArgs(rawArgs);

const commands = {
    start:    cmdStart,
    stop:     cmdStop,
    restart:  cmdRestart,
    status:   cmdStatus,
    verify:   cmdVerify,
    recreate: cmdRecreate,
    log:      cmdLog,
    root:     cmdRoot,
};

if (!commands[args.cmd]) {
    console.error(`Unknown command: ${args.cmd}`);
    usage();
}

Promise.resolve(commands[args.cmd](args)).catch(e => {
    die(e.message ?? String(e));
});
