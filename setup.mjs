#!/usr/bin/env node
/**
 * setup.mjs -- one-time codesearch setup.
 *
 * 1. Registers the MCP server with Claude Code
 * 2. Sets the VS Code tscodesearch.repoPath setting
 * 3. Creates a .client-venv with all Python deps (incl. tantivy)
 * 4. Creates config.json if absent
 * 5. Builds and installs the VS Code extension
 *
 * Usage:
 *   node setup.mjs
 *   node setup.mjs --uninstall
 */

import { spawnSync }                                    from 'node:child_process';
import { existsSync, readFileSync, writeFileSync,
         unlinkSync, linkSync, copyFileSync,
         readSync }                                     from 'node:fs';
import { join, dirname }                               from 'node:path';
import { fileURLToPath }                               from 'node:url';
import { randomBytes }                                 from 'node:crypto';

const REPO = dirname(fileURLToPath(import.meta.url));

// -- Helpers -------------------------------------------------------------------

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

function prompt(question) {
  process.stdout.write(question);
  const buf = Buffer.alloc(4096);
  let input = '';
  while (true) {
    let n;
    try { n = readSync(0, buf, 0, 1, null); } catch { break; }
    if (!n) break;
    const ch = buf.toString('utf8', 0, n);
    if (ch === '\n') break;
    if (ch !== '\r') input += ch;
  }
  return input.trim();
}

// -- Argument parsing ----------------------------------------------------------

const argv      = process.argv.slice(2);
const uninstall = argv.includes('--uninstall');

// -- Uninstall -----------------------------------------------------------------

if (uninstall) {
  console.log('Stopping daemon...');
  run('node', [join(REPO, 'ts.mjs'), 'stop'], { stdio: 'pipe' });
  console.log('Removing codesearch MCP server...');
  if (run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch']) !== 0)
    console.log('WARNING: claude mcp remove failed (may not have been registered).');
  else
    console.log('  Removed from Claude Code.');
  // Remove from VS Code / GitHub Copilot settings
  const _vsPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'settings.json');
  try {
    const _s = JSON.parse(readFileSync(_vsPath, 'utf8'));
    if (_s?.mcp?.servers?.tscodesearch) {
      delete _s.mcp.servers.tscodesearch;
      writeFileSync(_vsPath, JSON.stringify(_s, null, 2) + '\n', 'utf8');
      console.log('  Removed from VS Code MCP settings.');
    }
  } catch { /* settings.json missing or unreadable -- nothing to remove */ }
  console.log('Done. Reload VS Code / Claude Code for the change to take effect.');
  process.exit(0);
}

// -- Main setup ----------------------------------------------------------------

// [1] Register MCP
step(1, 'Registering MCP server (Claude Code + GitHub Copilot)');
run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch'], { stdio: 'pipe' });
if (run('claude', [
  'mcp', 'add', '--scope', 'user', 'tscodesearch',
  '--', join(REPO, '.client-venv', 'Scripts', 'python.exe'), join(REPO, 'mcp_server.py'),
]) === 0)
  console.log('  Registered with Claude Code.');
else
  console.log('  WARNING: claude mcp add failed -- Claude Code may not be installed.');

// Register with VS Code (GitHub Copilot) and set tscodesearch.repoPath
const vscodeSettingsPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'settings.json');
try {
  let settings = {};
  if (existsSync(vscodeSettingsPath)) {
    try { settings = JSON.parse(readFileSync(vscodeSettingsPath, 'utf8')); } catch {}
  }
  settings['tscodesearch.repoPath'] = REPO;
  if (!settings.mcp) settings.mcp = {};
  if (!settings.mcp.servers) settings.mcp.servers = {};
  settings.mcp.servers.tscodesearch = {
    type: 'stdio',
    command: join(REPO, '.client-venv', 'Scripts', 'python.exe'),
    args: [join(REPO, 'mcp_server.py')],
  };
  writeFileSync(vscodeSettingsPath, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  console.log('  Registered with VS Code (GitHub Copilot).');
  console.log(`  Set tscodesearch.repoPath to ${REPO}.`);
} catch (e) {
  console.log(`  WARNING: Could not update VS Code settings: ${e.message}`);
  console.log('  Add manually to VS Code settings.json:');
  console.log(`    "mcp": { "servers": { "tscodesearch": { "type": "stdio",`);
  console.log(`      "command": "${join(REPO, '.client-venv', 'Scripts', 'python.exe')}",`);
  console.log(`      "args": ["${join(REPO, 'mcp_server.py')}"] } } }`);
}

// [2] Client venv (managed Python via uv)
step(2, 'Creating client venv (.client-venv)');
{
  const clientVenv = join(REPO, '.client-venv');
  const pyExe      = join(clientVenv, 'Scripts', 'python.exe');
  const reqs       = join(REPO, 'requirements-client.txt');
  const PYTHON_VER = '3.12';

  if (!commandExists('uv')) {
    console.log('  uv not found -- installing via winget...');
    if (run('winget', ['install', '--id', 'astral-sh.uv', '-e', '--silent']) !== 0)
      die('uv install failed. Install uv manually (https://docs.astral.sh/uv/) then re-run setup.');
    if (!commandExists('uv'))
      die('uv installed but not yet in PATH -- open a new terminal and re-run setup.');
  }

  runOrDie('uv', ['python', 'install', PYTHON_VER], `uv python install ${PYTHON_VER}`);

  const needsCreate = !existsSync(pyExe) || (() => {
    const v = capture(pyExe, ['--version']);
    const m = v?.match(/^Python 3\.(\d+)/);
    return !m || parseInt(m[1], 10) < 10;
  })();

  if (needsCreate) {
    if (existsSync(pyExe)) console.log('  Python version too old -- recreating venv...');
    runOrDie('uv', ['venv', '--python', PYTHON_VER, clientVenv], 'uv venv');
  }

  console.log(needsCreate ? '  Installing packages...' : '  Updating packages...');
  runOrDie('uv', ['pip', 'install', '--quiet', '--upgrade', '-r', reqs],
    'uv pip install', { env: { ...process.env, VIRTUAL_ENV: clientVenv } });
  console.log('  Done.');

  // Create tscodesearch.exe as a hard link to python.exe so the daemon
  // shows a descriptive process name in Task Manager instead of "python.exe".
  const tsExe = join(clientVenv, 'Scripts', 'tscodesearch.exe');
  if (needsCreate || !existsSync(tsExe)) {
    if (existsSync(tsExe)) unlinkSync(tsExe);
    try {
      linkSync(pyExe, tsExe);
      console.log('  Created tscodesearch.exe (hard link for daemon process name).');
    } catch {
      copyFileSync(pyExe, tsExe);
      console.log('  Copied tscodesearch.exe (daemon process name alias).');
    }
  }
}

// [3] Create config.json if absent
step(3, 'config.json');
if (existsSync(join(REPO, 'config.json'))) {
  console.log('  Already exists, skipping.');
} else {
  let rootPath = '';
  if (process.stdin.isTTY) {
    rootPath = prompt('  Source directory to index (leave blank to add later): ');
    // Strip surrounding quotes and normalise slashes
    rootPath = rootPath.replace(/^["']|["']$/g, '').replace(/\\/g, '/').trim();
    if (rootPath && !existsSync(rootPath)) {
      console.log(`  WARNING: '${rootPath}' does not exist -- skipping.`);
      console.log('  Add roots later with:  ts root --add default <path>');
      rootPath = '';
    }
  }
  const roots = rootPath ? { default: { path: rootPath } } : {};
  const config = { api_key: randomBytes(20).toString('hex'), port: 8108, roots };
  writeFileSync(join(REPO, 'config.json'), JSON.stringify(config, null, 2) + '\n', 'utf8');
  if (rootPath)
    console.log(`  Created. Indexing root: ${rootPath}`);
  else
    console.log('  Created (no root set). Add roots later:  ts root --add default <path>');
}

// [4] VS Code extension
step(4, 'Installing VS Code extension');
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

// -- Done ---------------------------------------------------------------------

console.log('\n-- Setup complete! ----------------------------------------------------------\n');
console.log('Next: start the daemon:');
console.log('  ts start');
console.log('\nThen open VS Code (or reload: Ctrl+Shift+P > Reload Window) and:');
console.log('  TsCodeSearch: Add Root  -- to add source directories to index');
console.log('\nDaemon management:  ts start / ts stop / ts restart / ts status');
