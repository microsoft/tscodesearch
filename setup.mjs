#!/usr/bin/env node
/**
 * setup.mjs — one-time codesearch setup.
 *
 * Installs everything and starts the service so you can open VS Code
 * and immediately add source roots via the TsCodeSearch extension.
 *
 * Usage:
 *   node setup.mjs              Docker mode (default; requires Docker Desktop)
 *   node setup.mjs --wsl        WSL mode (installs venv + Typesense binary in WSL)
 *   node setup.mjs --uninstall  Unregister MCP server and stop service
 */

import { spawnSync }                                    from 'node:child_process';
import { existsSync, readFileSync, writeFileSync,
         unlinkSync }                                   from 'node:fs';
import { join, dirname }                               from 'node:path';
import { fileURLToPath }                               from 'node:url';
import { randomBytes }                                 from 'node:crypto';

const REPO = dirname(fileURLToPath(import.meta.url));

// ── Helpers ───────────────────────────────────────────────────────────────────

// On Windows, npm/claude/code etc. are .cmd batch wrappers that must be run
// via cmd.exe — they can't be spawned directly as executables, and using
// shell:true triggers a Node 22+ deprecation warning when args are an array.
const WIN_SCRIPTS = new Set(['npm', 'npx', 'claude', 'code', 'vsce']);

function resolveSpawn(cmd, args) {
  if (process.platform === 'win32' && WIN_SCRIPTS.has(cmd))
    return { cmd: 'cmd.exe', args: ['/c', cmd, ...args] };
  return { cmd, args };
}

function run(cmd, args, opts = {}) {
  const s = resolveSpawn(cmd, args);
  const r = spawnSync(s.cmd, s.args, { stdio: 'inherit', ...opts });
  if (r.error) throw r.error;
  return r.status ?? 1;
}

function runOrDie(cmd, args, label, opts = {}) {
  if (run(cmd, args, opts) !== 0) die(`${label} failed.`);
}

function capture(cmd, args, opts = {}) {
  const s = resolveSpawn(cmd, args);
  const r = spawnSync(s.cmd, s.args, { encoding: 'utf8', stdio: 'pipe', ...opts });
  if (r.error || r.status !== 0) return null;
  return r.stdout.trim();
}

function die(msg) { console.error(`ERROR: ${msg}`); process.exit(1); }

function step(n, label) { console.log(`\n[${n}] ${label}...`); }

function commandExists(cmd) {
  const checker = process.platform === 'win32' ? 'where' : 'which';
  return capture(checker, [cmd]) !== null;
}

const toWslPath = p =>
  p.replace(/\\/g, '/').replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`);

// ── Config ────────────────────────────────────────────────────────────────────

function readConfig() {
  const f = join(REPO, 'config.json');
  if (!existsSync(f)) return null;
  try { return JSON.parse(readFileSync(f, 'utf8')); } catch { return null; }
}

// ── Argument parsing ──────────────────────────────────────────────────────────

const argv      = process.argv.slice(2);
const uninstall = argv.includes('--uninstall');
const wslFlag   = argv.includes('--wsl');

// Existing config.json mode wins over CLI flag
const existingCfg = readConfig();
const mode        = existingCfg?.mode ?? (wslFlag ? 'wsl' : 'docker');
const isWsl       = mode === 'wsl';

// ── Uninstall ─────────────────────────────────────────────────────────────────

if (uninstall) {
  console.log('Stopping service...');
  run('node', [join(REPO, 'ts.mjs'), 'stop'], { stdio: 'pipe' });
  console.log('Removing codesearch MCP server...');
  if (run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch']) !== 0)
    console.log('WARNING: mcp remove failed (may not have been registered).');
  else
    console.log('Done. Restart Claude Code for the change to take effect.');
  process.exit(0);
}

// ── Main setup ────────────────────────────────────────────────────────────────

console.log(`Mode: ${mode}`);

// [1] Build MCP server
step(1, 'Building MCP server');
runOrDie('npm', ['install', '--no-fund', '--no-audit'], 'npm install', { cwd: REPO });
runOrDie('npm', ['run', 'build'], 'TypeScript build', { cwd: REPO });
console.log('  Done.');

// [2] Register MCP
step(2, 'Registering MCP server with Claude Code');
run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch'], { stdio: 'pipe' });
runOrDie('claude', [
  'mcp', 'add', '--scope', 'user', 'tscodesearch',
  '--', 'node.exe', join(REPO, 'mcp_server.js'),
], 'claude mcp add');

// Update VS Code tscodesearch.repoPath
const vscodeSettingsPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'settings.json');
try {
  let settings = {};
  if (existsSync(vscodeSettingsPath)) {
    try { settings = JSON.parse(readFileSync(vscodeSettingsPath, 'utf8')); } catch {}
  }
  settings['tscodesearch.repoPath'] = REPO;
  writeFileSync(vscodeSettingsPath, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  console.log(`  Set tscodesearch.repoPath to ${REPO}`);
} catch (e) {
  console.log(`  WARNING: Could not update VS Code settings: ${e.message}`);
  console.log(`  Set tscodesearch.repoPath manually to ${REPO}`);
}

// [3] WSL environment (wsl mode only)
if (isWsl) {
  step(3, 'Setting up WSL environment (venv + Typesense binary)');
  const wslRepo = toWslPath(REPO);
  runOrDie('wsl.exe', ['bash', '-lc', `bash '${wslRepo}/scripts/wsl-setup.sh'`], 'WSL setup');
  console.log('  Done.');
}

// [4] Create config.json if absent
step(4, 'config.json');
if (existsSync(join(REPO, 'config.json'))) {
  console.log('  Already exists, skipping.');
} else {
  const config = { api_key: randomBytes(20).toString('hex'), port: 8108, mode, roots: {} };
  writeFileSync(join(REPO, 'config.json'), JSON.stringify(config, null, 2) + '\n', 'utf8');
  console.log(`  Created with mode=${mode} (api_key auto-generated).`);
}

// [5] VS Code extension
step(5, 'Installing VS Code extension');
const vscodeDir = join(REPO, 'vscode-codesearch');
if (!existsSync(join(vscodeDir, 'package.json'))) {
  console.log('  SKIPPED: vscode-codesearch directory not found.');
} else if (!commandExists('code')) {
  console.log("  SKIPPED: 'code' not found in PATH.");
  console.log(`  Install manually: VS Code > F1 > Developer: Install Extension from Location > ${vscodeDir}`);
} else {
  runOrDie('npm', ['install', '--no-fund', '--no-audit'], 'npm install (vscode)', { cwd: vscodeDir });
  runOrDie('npm', ['run', 'compile'], 'compile', { cwd: vscodeDir });
  runOrDie('npm', ['run', 'package', '--', '-o', 'codesearch.vsix'], 'package', { cwd: vscodeDir });
  const vsix = join(vscodeDir, 'codesearch.vsix');
  const installOk = run('code', ['--install-extension', vsix]) === 0;
  try { if (existsSync(vsix)) unlinkSync(vsix); } catch {}
  if (!installOk) {
    console.log('  WARNING: VS Code extension install failed.');
    console.log(`  Install manually: VS Code > F1 > Developer: Install Extension from Location > ${vscodeDir}`);
  } else {
    console.log('  Done.');
  }
}

// ── Done ─────────────────────────────────────────────────────────────────────

console.log('\n── Setup complete! ──────────────────────────────────────────────────────────\n');
console.log('Next: start the service:');
console.log(isWsl ? '  ts start' : '  ts start  (builds Docker image on first run — may take a few minutes)');
console.log('\nThen open VS Code (or reload: Ctrl+Shift+P > Reload Window) and:');
console.log('  TsCodeSearch: Add Root  — to add source directories to index');
console.log('\nService management:  ts start / ts stop / ts restart / ts status');
