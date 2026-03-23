#!/usr/bin/env node
/**
 * ts.mjs — codesearch management CLI
 *
 * Usage: ts <command> [options]
 *
 * Commands:
 *   start                  Start the server (Docker: create/start container; WSL: start services)
 *   stop                   Stop the server
 *   restart                Stop then start
 *   status                 Show service health and index statistics
 *   index [--resethard]    Run indexer (--resethard: wipe and reindex from scratch)
 *         [--root NAME]
 *   verify [--root NAME]   Scan file system and repair stale/missing index entries
 *          [--no-delete-orphans]
 *   log [-n N]             Show server log (Docker: container logs; WSL: server log)
 *       [--indexer]        WSL only: show indexer log
 *       [--error]          WSL only: show server error log
 *   root                   List configured roots
 *   root --add NAME PATH   Add (or update) a root in config.json
 *   root --remove NAME     Remove a root from config.json
 *   build                  Docker only: build the Docker image
 *   setup                  Docker: build image if needed + start container
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync, spawn } from 'child_process';
import http from 'http';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// ── Config ────────────────────────────────────────────────────────────────────

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

const cfg       = readConfig();
const API_KEY   = cfg.api_key   ?? 'codesearch-local';
const TS_PORT   = cfg.port      ?? 8108;
const API_PORT  = TS_PORT + 1;
const MODE      = (cfg.mode     ?? 'docker');   // 'docker' | 'wsl'
const CONTAINER = cfg.docker_container ?? 'codesearch';
const IMAGE     = cfg.docker_image     ?? 'codesearch-mcp';
const DATA_VOL  = `${CONTAINER}_data`;
const ROOTS     = cfg.roots ?? {};

// ── Helpers ───────────────────────────────────────────────────────────────────

function log(msg)  { console.log(`[ts] ${msg}`); }
function die(msg)  { console.error(`[ts] ERROR: ${msg}`); process.exit(1); }

/** Windows path → WSL /mnt/<drive>/... */
function winToWsl(p) {
    return p.replace(/\\/g, '/').replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`);
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

function apiGet(urlPath, timeoutMs = 5000) {
    return new Promise((resolve, reject) => {
        const req = http.request({
            host: 'localhost', port: API_PORT, path: urlPath, method: 'GET',
            headers: { 'X-TYPESENSE-API-KEY': API_KEY },
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
            host: 'localhost', port: API_PORT, path: urlPath, method: 'POST',
            headers: {
                'X-TYPESENSE-API-KEY': API_KEY,
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

// ── Docker helpers ────────────────────────────────────────────────────────────

function docker(args, opts = {}) {
    return spawnSync('docker', args, {
        stdio: opts.silent ? 'pipe' : 'inherit',
        encoding: 'utf-8',
        ...opts,
    });
}

function dockerCapture(args) {
    return spawnSync('docker', args, { encoding: 'utf-8', stdio: 'pipe' });
}

function containerExists() {
    return dockerCapture(['inspect', '--format', '{{.Name}}', CONTAINER]).status === 0;
}

function containerIsRunning() {
    const r = dockerCapture(['inspect', '--format', '{{.State.Running}}', CONTAINER]);
    return r.status === 0 && r.stdout.trim() === 'true';
}

/**
 * Read /source/<name> bind mounts from a running/stopped container.
 * Returns { name: wslSourcePath } or null if the container does not exist.
 */
function getContainerRootMounts() {
    const r = dockerCapture(['inspect', '--format', '{{json .Mounts}}', CONTAINER]);
    if (r.status !== 0) return null;
    try {
        const mounts = JSON.parse(r.stdout.trim());
        const result = {};
        for (const m of mounts) {
            const match = m.Destination?.match(/^\/source\/(.+)$/);
            if (match) result[match[1]] = m.Source;
        }
        return result;
    } catch { return null; }
}

/**
 * Returns true if the container's /source/* bind mounts no longer match
 * config.json roots (root added, removed, or path changed).
 */
function rootsMismatch() {
    const actual = getContainerRootMounts();
    if (!actual) return false;  // container doesn't exist

    const expected = {};
    for (const [name, entry] of Object.entries(ROOTS)) {
        const winPath = entry.windows_path ?? '';
        expected[name] = winToWsl(winPath.replace(/\\/g, '/')).replace(/\/+$/, '');
    }

    const aKeys = Object.keys(actual).sort().join(',');
    const eKeys = Object.keys(expected).sort().join(',');
    if (aKeys !== eKeys) return true;

    for (const name of Object.keys(expected)) {
        if (actual[name]?.replace(/\/$/, '') !== expected[name]) return true;
    }
    return false;
}

/**
 * Write a container-internal config (roots mapped to /source/<name>) to a
 * fixed path next to config.json.  This file must persist for the lifetime
 * of the container because it is bind-mounted into it as a live mount.
 *
 * Each root entry is an object with:
 *   local_path   — path inside the container (/source/<name>)
 *   windows_path — original Windows path on the host (C:/repos/src)
 * This lets the indexer store host-side filenames without needing a
 * separate --root argument or a host_roots lookup table.
 */
function writeContainerConfig() {
    const containerRoots = Object.fromEntries(
        Object.entries(ROOTS).map(([name, entry]) => [
            name,
            {
                local_path:   `/source/${name}`,
                windows_path: (entry.windows_path ?? '').replace(/\\/g, '/').replace(/\/+$/, ''),
            },
        ])
    );
    const content = JSON.stringify({ api_key: API_KEY, port: TS_PORT, roots: containerRoots }, null, 2);
    const dest = path.join(__dirname, 'config.container.json');
    fs.writeFileSync(dest, content, 'utf-8');
    return dest;
}

/** Build docker run args and create + start the container. */
function dockerCreate(configFile) {
    const args = [
        'run', '-d',
        '--name', CONTAINER,
        '-p', `${API_PORT}:8109`,
        '-e', 'CODESEARCH_API_HOST=0.0.0.0',
        '-v', `${configFile}:/app/config.json:ro`,
        '-v', `${DATA_VOL}:/typesensedata`,
        '-v', `${__dirname}/scripts:/app/scripts:ro`,
    ];
    for (const [name, entry] of Object.entries(ROOTS)) {
        args.push('-v', `${entry.windows_path ?? ''}:/source/${name}:ro`);
    }
    args.push(IMAGE);
    return docker(args);
}

/** Stream container logs until "Ready for connections" or timeout. */
function waitForReady() {
    return new Promise(resolve => {
        log('Streaming logs until ready...');
        const proc = spawn('docker', ['logs', '-f', CONTAINER], { stdio: 'pipe' });
        let done = false;

        const finish = (msg) => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            proc.kill('SIGTERM');
            if (msg) log(msg);
            resolve();
        };

        const timer = setTimeout(
            () => finish(`WARNING: Server did not reach ready state within 5 min — check 'docker logs ${CONTAINER}'.`),
            300_000
        );

        const onData = (data) => {
            const text = data.toString();
            process.stdout.write(text);
            if (text.includes('Ready for connections')) {
                finish('Management API is up. Typesense may still be loading — run: ts status');
            }
        };

        proc.stdout.on('data', onData);
        proc.stderr.on('data', onData);
        proc.on('close', () => finish(''));
    });
}

// ── WSL mode helper ───────────────────────────────────────────────────────────

function wslRun(cmd, extraArgs = []) {
    const repoWsl   = winToWsl(__dirname);
    const venvPy    = '~/.local/indexserver-venv/bin/python3';
    const servicePy = `${repoWsl}/indexserver/service.py`;
    const cmdLine   = [venvPy, servicePy, cmd, ...extraArgs].join(' ');
    const r = spawnSync('wsl.exe', ['bash', '-lc', cmdLine], {
        stdio: 'inherit', encoding: 'utf-8',
    });
    if (r.status !== 0) process.exit(r.status ?? 1);
}

// ── Status display (Docker mode) ──────────────────────────────────────────────

function fmtNum(n)  { return n == null ? '?' : Number(n).toLocaleString(); }
function fmtTs(ts)  { return ts ? ts.replace('T', ' ').substring(0, 16) : ''; }

function printDockerStatus(apiBody) {
    const collections = apiBody?.collections ?? {};
    const syncer      = apiBody?.syncer      ?? {};
    const watcher     = apiBody?.watcher     ?? {};
    const queue       = apiBody?.queue       ?? {};
    const prog        = syncer.progress      ?? {};
    const syncerRunning = syncer.running ?? false;

    // queue is actively indexing if syncer placed files and queue worker is writing
    const qDepth    = queue.depth    ?? 0;
    const qEnqueued = queue.enqueued ?? 0;
    const qUpserted = queue.upserted ?? 0;
    const qDeduped  = queue.deduped  ?? 0;
    const qSkipped  = queue.skipped  ?? 0;
    const qDeleted  = queue.deleted  ?? 0;
    const qErrors   = queue.errors   ?? 0;
    const isQueued  = prog.status === 'queued' && qDepth > 0;

    // ── Per-root index status ────────────────────────────────────────────────
    for (const [rootName, info] of Object.entries(collections)) {
        const ndocs    = info?.num_documents;
        const warnings = info?.schema_warnings ?? [];
        const exists   = info?.collection_exists;
        const synced   = info?.synced;
        const syncedAt = info?.synced_at;
        const isCurrent = prog.collection && prog.collection === info?.collection;

        let badge, detail;
        if (!exists || ndocs == null) {
            badge  = '[--]';
            detail = 'not yet indexed — run: ts index';
        } else if (warnings.length > 0) {
            badge  = '[!!]';
            detail = `schema outdated (${fmtNum(ndocs)} docs) — run: ts index --root ${rootName} --resethard`;
        } else if (isCurrent && (syncerRunning || isQueued)) {
            const total = qEnqueued > 0 ? qEnqueued : (prog.total_to_update ?? 0);
            const done  = qUpserted;
            const pct   = total > 0 ? ` ${Math.floor(done * 100 / total)}%` : '';
            badge  = '[>>]';
            detail = `${fmtNum(ndocs)} docs  indexing  ${fmtNum(done)}/${fmtNum(total)}${pct}`;
        } else if (!synced) {
            badge  = '[~~]';
            detail = `${fmtNum(ndocs)} docs  incomplete sync — run: ts index --root ${rootName}`;
        } else {
            const when = syncedAt ? `  synced ${fmtTs(syncedAt)}` : '';
            badge  = '[OK]';
            detail = `${fmtNum(ndocs)} docs${when}`;
        }
        console.log(`  [${rootName}] Index  : ${badge} ${detail}`);
        for (const w of warnings) console.log(`               ${w}`);
    }

    // ── Syncer / indexer progress ────────────────────────────────────────────
    if (syncerRunning || isQueued || (syncer.pending ?? 0) > 0) {
        const phase     = prog.phase    ?? 'starting';
        const fsFiles   = prog.fs_files ?? 0;
        const idxDocs   = prog.index_docs ?? 0;
        const missing   = prog.missing  ?? 0;
        const stale     = prog.stale    ?? 0;
        const orphaned  = prog.orphaned ?? 0;
        const total     = qEnqueued > 0 ? qEnqueued : (prog.total_to_update ?? 0);
        const done      = qUpserted;
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

    // ── Queue stats ──────────────────────────────────────────────────────────
    if (qEnqueued > 0 || qDepth > 0) {
        const errStr = qErrors > 0 ? `  errors=${qErrors}` : '';
        console.log(`  Queue   : depth=${fmtNum(qDepth)}  enqueued=${fmtNum(qEnqueued)}  upserted=${fmtNum(qUpserted)}  skipped=${fmtNum(qSkipped)}  deduped=${fmtNum(qDeduped)}  deleted=${fmtNum(qDeleted)}${errStr}`);
    }

    // ── Watcher ──────────────────────────────────────────────────────────────
    const state  = watcher.state ?? (watcher.running ? 'watching' : 'stopped');
    const watchQD = watcher.queue_depth ?? 0;
    if (state === 'watching') {
        console.log(`  Watcher : [OK] watching`);
    } else if (state === 'paused') {
        console.log(`  Watcher : [OK] paused (VS Code watcher active)`);
    } else if (state === 'processing') {
        console.log(`  Watcher : [>>] processing  queue_depth=${watchQD}`);
    } else {
        console.log(`  Watcher : [--] stopped`);
    }

    // ── Typesense health ─────────────────────────────────────────────────────
    const tsOk      = apiBody?.typesense_ok;
    const tsLoading = apiBody?.typesense_loading;
    if (tsLoading)           console.log(`  Typesense: [..] loading`);
    else if (tsOk === false) console.log(`  Typesense: [!!] unhealthy`);
}

// ── Commands ──────────────────────────────────────────────────────────────────

async function cmdStart() {
    if (MODE === 'wsl') { wslRun('start'); return; }

    const info = dockerCapture(['info', '--format', '{{.ID}}']);
    if (info.status !== 0) die('Docker is not running. Start Docker Desktop and try again.');

    ensureImage();

    if (containerExists()) {
        if (rootsMismatch()) {
            log(`Root mismatch detected — recreating container '${CONTAINER}'...`);
            docker(['stop', CONTAINER], { silent: true });
            docker(['rm',   CONTAINER], { silent: true });
            // fall through to create
        } else if (containerIsRunning()) {
            log(`Container '${CONTAINER}' is already running.`);
            await cmdStatus();
            return;
        } else {
            log(`Starting existing container '${CONTAINER}'...`);
            const r = docker(['start', CONTAINER]);
            if (r.status !== 0) die('docker start failed.');
            await waitForReady();
            return;
        }
    }

    log(`Creating container '${CONTAINER}'...`);
    const configFile = writeContainerConfig();
    const r = dockerCreate(configFile);
    if (r.status !== 0) die('docker run failed.');
    await waitForReady();
}

async function cmdStop() {
    if (MODE === 'wsl') { wslRun('stop'); return; }
    log(`Stopping container '${CONTAINER}'...`);
    docker(['stop', CONTAINER], { silent: true });
    docker(['rm',   CONTAINER], { silent: true });
    log('Done.');
}

async function cmdRestart() {
    if (MODE === 'wsl') { wslRun('restart'); return; }

    const info = dockerCapture(['info', '--format', '{{.ID}}']);
    if (info.status !== 0) die('Docker is not running. Start Docker Desktop and try again.');

    ensureImage();

    if (containerExists()) {
        if (rootsMismatch()) {
            // Roots changed — need full recreate
            log(`Root mismatch detected — recreating container '${CONTAINER}'...`);
            docker(['stop', CONTAINER], { silent: true });
            docker(['rm',   CONTAINER], { silent: true });
            const configFile = writeContainerConfig();
            const r = dockerCreate(configFile);
            if (r.status !== 0) die('docker run failed.');
        } else {
            // Normal restart: stop + start, preserving the container
            log(`Restarting container '${CONTAINER}'...`);
            docker(['stop', CONTAINER], { silent: true });
            const r = docker(['start', CONTAINER]);
            if (r.status !== 0) die('docker start failed.');
            log('Done.');
            return;
        }
    } else {
        // No container — treat as start
        const configFile = writeContainerConfig();
        log(`Creating container '${CONTAINER}'...`);
        const r = dockerCreate(configFile);
        if (r.status !== 0) die('docker run failed.');
    }

    await waitForReady();
}

async function cmdStatus() {
    if (MODE === 'wsl') { wslRun('status'); return; }

    console.log(`-- Codesearch Status (Docker) ----------------------------------------`);
    const running = containerIsRunning();
    console.log(`  Container: ${running ? '[OK]  running' : '[--] stopped'}  (${CONTAINER})`);

    if (!running) {
        console.log(`----------------------------------------------------------------------`);
        return;
    }

    try {
        const { status, body } = await apiGet('/status');
        if (status === 200 && typeof body === 'object') {
            printDockerStatus(body);
        } else {
            console.log('  API     : not responding');
        }
    } catch {
        console.log('  API     : not responding (indexserver may still be starting)');
    }
    console.log(`----------------------------------------------------------------------`);
}

async function cmdIndex(args) {
    if (MODE === 'wsl') {
        const extra = [];
        if (args.resethard)          extra.push('--resethard');
        if (args.root)               extra.push('--root', args.root);
        wslRun('index', extra);
        return;
    }

    const rootName = args.root || Object.keys(ROOTS)[0] || 'default';
    if (args.resethard) {
        // For resethard in Docker mode, stop+rm the container and restart (wipes volume)
        log('Hard reset: stopping container...');
        docker(['stop', CONTAINER], { silent: true });
        docker(['rm',   CONTAINER], { silent: true });
        log('Removing data volume...');
        docker(['volume', 'rm', DATA_VOL], { silent: true });
        log('Starting fresh...');
        await cmdStart();
        // After start, trigger indexer
    }

    try {
        const { status, body } = await apiPost('/index/start', {
            root: rootName, resethard: !!args.resethard,
        });
        if (status === 409) { log('Indexer already running. Monitor with: ts status'); return; }
        if (status !== 200) die(`indexserver returned ${status}: ${body?.error ?? JSON.stringify(body)}`);
        log(`Indexer started for root '${rootName}'. Monitor with: ts status`);
    } catch (e) {
        die(`Cannot reach indexserver: ${e.message}`);
    }
}

async function cmdVerify(args) {
    if (MODE === 'wsl') {
        const extra = [];
        if (args.root)             extra.push('--root', args.root);
        if (args.noDeleteOrphans)  extra.push('--no-delete-orphans');
        wslRun('verify', extra);
        return;
    }

    const rootName = args.root || Object.keys(ROOTS)[0] || 'default';
    try {
        const { status, body } = await apiPost('/verify/start', {
            root: rootName, delete_orphans: !args.noDeleteOrphans,
        });
        if (status === 409) { log('Verifier already running. Monitor with: ts status'); return; }
        if (status !== 200) die(`indexserver returned ${status}: ${body?.error ?? JSON.stringify(body)}`);
        log(`Verification started for root '${rootName}'. Monitor with: ts status`);
    } catch (e) {
        die(`Cannot reach indexserver: ${e.message}`);
    }
}

function cmdLog(args) {
    if (MODE === 'wsl') {
        const extra = [];
        if (args.indexer)  extra.push('--indexer');
        if (args.error)    extra.push('--error');
        extra.push('-n', String(args.lines ?? 40));
        wslRun('log', extra);
        return;
    }

    const dockerArgs = ['logs', '--tail', String(args.lines ?? 40)];
    if (args.follow) dockerArgs.push('-f');
    dockerArgs.push(CONTAINER);
    docker(dockerArgs);
}

function cmdBuild() {
    const dockerfile = path.join(__dirname, 'docker', 'Dockerfile');
    if (!fs.existsSync(dockerfile)) die(`Dockerfile not found: ${dockerfile}`);
    log(`Building image '${IMAGE}'...`);
    const r = docker(['build', '-t', IMAGE, '-f', dockerfile, __dirname]);
    if (r.status !== 0) die('docker build failed.');
    log('Image built.');
}

/** Build the image if it does not exist locally. */
function ensureImage() {
    const r = dockerCapture(['images', '-q', IMAGE]);
    if (!r.stdout.trim()) {
        log(`Image '${IMAGE}' not found — building...`);
        cmdBuild();
    }
}

async function cmdSetup() {
    // setup = ensure image + start; ensureImage() is called inside cmdStart
    await cmdStart();
}

function cmdRoot(args) {
    const current = readConfig();
    const roots   = current.roots ?? {};

    if (args.addName) {
        if (!args.addPath) die('--add requires NAME and PATH');
        const p = args.addPath.replace(/\\/g, '/').replace(/\/+$/, '');
        const entry = { windows_path: p };
        // In WSL mode, also store the server-local path so the indexserver can
        // find files without having to auto-derive it at runtime.
        if (MODE === 'wsl') entry.local_path = winToWsl(p);
        roots[args.addName] = entry;
        current.roots = roots;
        saveConfig(current);
        log(`Root '${args.addName}' = ${p}`);
        if (MODE === 'wsl') log(`  local_path = ${entry.local_path}`);
        log('Restart the server for the change to take effect: ts restart');
        return;
    }

    if (args.removeName) {
        if (!(args.removeName in roots)) die(`Root '${args.removeName}' not found`);
        delete roots[args.removeName];
        current.roots = roots;
        saveConfig(current);
        log(`Root '${args.removeName}' removed.`);
        log('Restart the server for the change to take effect: ts restart');
        return;
    }

    // List
    const names = Object.keys(roots);
    if (!names.length) {
        console.log('No roots configured.');
        return;
    }
    console.log('Configured roots:');
    for (const [name, entry] of Object.entries(roots)) {
        const p = (entry && typeof entry === 'object') ? (entry.windows_path ?? JSON.stringify(entry)) : entry;
        console.log(`  ${name.padEnd(16)} ${p}`);
    }
}

// ── Argument parsing ──────────────────────────────────────────────────────────

function usage() {
    console.log(`
Usage: ts <command> [options]

Commands:
  start                  Start the server
  stop                   Stop the server
  restart                Stop then start
  status                 Show service health and index stats
  index                  Run the indexer
    --resethard          Wipe all data and reindex from scratch
    --root NAME          Root to index (default: first configured root)
  verify                 Scan file system and repair index
    --root NAME          Root to verify (default: first configured root)
    --no-delete-orphans  Keep index entries for deleted files
  log                    Show server log
    -n N                 Number of lines (default: 40)
    -f, --follow         Follow log output
    --indexer            WSL: show indexer log
    --error              WSL: show server error log
  root                   List configured roots
  root --add NAME PATH   Add (or update) a root in config.json
  root --remove NAME     Remove a root from config.json
  build                  Docker only: build the Docker image
  setup                  Build image if needed, then start
`.trim());
    process.exit(0);
}

function parseArgs(argv) {
    const [cmd, ...rest] = argv;
    const args = {
        cmd, root: null, resethard: false, noDeleteOrphans: false,
        indexer: false, error: false, lines: 40, follow: false,
        addName: null, addPath: null, removeName: null,
    };
    for (let i = 0; i < rest.length; i++) {
        switch (rest[i]) {
            case '--resethard':           args.resethard = true; break;
            case '--root':                args.root = rest[++i]; break;
            case '--no-delete-orphans':   args.noDeleteOrphans = true; break;
            case '--indexer':             args.indexer = true; break;
            case '--error':               args.error = true; break;
            case '-f': case '--follow':   args.follow = true; break;
            case '-n': case '--lines':    args.lines = parseInt(rest[++i], 10) || 40; break;
            case '--add':
                args.addName = rest[++i];
                args.addPath = rest[++i];
                break;
            case '--remove':              args.removeName = rest[++i]; break;
            default:
                if (rest[i].startsWith('-')) {
                    console.error(`Unknown option: ${rest[i]}`);
                    process.exit(1);
                }
        }
    }
    return args;
}

// ── Main ──────────────────────────────────────────────────────────────────────

const rawArgs = process.argv.slice(2);
if (!rawArgs.length || rawArgs[0] === '--help' || rawArgs[0] === '-h') usage();

const args = parseArgs(rawArgs);

const commands = {
    start:   cmdStart,
    stop:    cmdStop,
    restart: cmdRestart,
    status:  cmdStatus,
    index:   cmdIndex,
    verify:  cmdVerify,
    log:     cmdLog,
    root:    cmdRoot,
    build:   () => cmdBuild(),
    setup:   cmdSetup,
};

if (!commands[args.cmd]) {
    console.error(`Unknown command: ${args.cmd}`);
    usage();
}

Promise.resolve(commands[args.cmd](args)).catch(e => {
    die(e.message ?? String(e));
});
