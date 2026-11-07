#!/usr/bin/env node
/**
 * setup.mjs -- one-time codesearch setup.
 *
 * 1. Registers the MCP server with Claude Code
 * 2. Sets the VS Code tscodesearch.repoPath setting
 * 3. Synchronizes the root .venv with all Python deps (incl. tantivy)
 * 4. Creates config.json if absent
 * 5. Builds and installs the VS Code extension
 *
 * Usage:
 *   node setup.mjs
 *   node setup.mjs --priority
 *   node setup.mjs --uninstall
 */

import { spawnSync }                                    from 'node:child_process';
import { existsSync, readFileSync, writeFileSync,
         unlinkSync,
         readSync, readdirSync, rmSync, mkdirSync }      from 'node:fs';
import { join, dirname }                               from 'node:path';
import { fileURLToPath }                               from 'node:url';
import { randomBytes }                                 from 'node:crypto';
import { homedir }                                     from 'node:os';
import { uvInvocation }                                from './lib/uv.mjs';
import { updateCopilotMcp }                             from './lib/copilot_mcp.mjs';
import { createDaemonExecutable }                      from './scripts/create_daemon_exe.mjs';

const REPO = dirname(fileURLToPath(import.meta.url));
const PRIORITY_START = '<!-- tscodesearch-priority:start -->';
const PRIORITY_END = '<!-- tscodesearch-priority:end -->';
const COPILOT_INSTRUCTIONS_PATH = join(homedir(), '.copilot', 'copilot-instructions.md');
const COPILOT_MCP_PATH = join(process.env.COPILOT_HOME || join(homedir(), '.copilot'), 'mcp-config.json');
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

function removeIncompletePackageMetadata(sitePackages) {
  if (!existsSync(sitePackages)) return;
  for (const entry of readdirSync(sitePackages, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.endsWith('.dist-info')) continue;
    const metadataDir = join(sitePackages, entry.name);
    if (existsSync(join(metadataDir, 'RECORD'))) continue;
    console.log(`  Removing incomplete package metadata: ${entry.name}`);
    rmSync(metadataDir, { recursive: true, force: true });
  }
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

function priorityInstruction(eol) {
  return [
    PRIORITY_START,
    '## CRITICAL: Prefer tscodesearch for local code',
    '',
    'When searching or reading source files available in the current local working tree or a configured tscodesearch root, use tscodesearch first and prefer it over Bluebird. Use Bluebird only when a query explicitly targets checked-in repository state or code in another source tree that is not available through tscodesearch.',
    PRIORITY_END,
  ].join(eol);
}

function addPriorityInstruction() {
  const existing = existsSync(COPILOT_INSTRUCTIONS_PATH)
    ? readFileSync(COPILOT_INSTRUCTIONS_PATH, 'utf8')
    : '';
  const eol = existing.includes('\r\n') ? '\r\n' : '\n';
  const start = existing.indexOf(PRIORITY_START);
  const end = existing.indexOf(PRIORITY_END);
  const duplicateStart = start !== -1 &&
    existing.indexOf(PRIORITY_START, start + PRIORITY_START.length) !== -1;
  const duplicateEnd = end !== -1 &&
    existing.indexOf(PRIORITY_END, end + PRIORITY_END.length) !== -1;
  if ((start === -1) !== (end === -1) || (start !== -1 && end < start) ||
      duplicateStart || duplicateEnd)
    throw new Error(`Malformed tscodesearch priority block in ${COPILOT_INSTRUCTIONS_PATH}.`);

  const instruction = priorityInstruction(eol);
  let updated;
  if (start !== -1) {
    updated = existing.slice(0, start) + instruction +
      existing.slice(end + PRIORITY_END.length);
  } else {
    const separator = existing.length === 0 ? '' : existing.endsWith(eol) ? eol : eol + eol;
    updated = existing + separator + instruction + eol;
  }

  mkdirSync(dirname(COPILOT_INSTRUCTIONS_PATH), { recursive: true });
  if (updated !== existing)
    writeFileSync(COPILOT_INSTRUCTIONS_PATH, updated, 'utf8');
  console.log(`  Added critical tscodesearch priority instruction to ${COPILOT_INSTRUCTIONS_PATH}.`);
}

function removePriorityInstruction() {
  if (!existsSync(COPILOT_INSTRUCTIONS_PATH)) return;
  const existing = readFileSync(COPILOT_INSTRUCTIONS_PATH, 'utf8');
  const start = existing.indexOf(PRIORITY_START);
  const end = existing.indexOf(PRIORITY_END);
  if (start === -1 && end === -1) return;
  const duplicateStart = start !== -1 &&
    existing.indexOf(PRIORITY_START, start + PRIORITY_START.length) !== -1;
  const duplicateEnd = end !== -1 &&
    existing.indexOf(PRIORITY_END, end + PRIORITY_END.length) !== -1;
  if (start === -1 || end < start || duplicateStart || duplicateEnd)
    throw new Error(`Malformed tscodesearch priority block in ${COPILOT_INSTRUCTIONS_PATH}.`);

  let blockStart = start;
  if (existing.slice(Math.max(0, start - 2), start) === '\r\n')
    blockStart -= 2;
  else if (existing[start - 1] === '\n')
    blockStart -= 1;

  let blockEnd = end + PRIORITY_END.length;
  if (existing.slice(blockEnd, blockEnd + 2) === '\r\n')
    blockEnd += 2;
  else if (existing[blockEnd] === '\n')
    blockEnd += 1;

  const updated = existing.slice(0, blockStart) + existing.slice(blockEnd);
  if (updated.trim() === '')
    unlinkSync(COPILOT_INSTRUCTIONS_PATH);
  else
    writeFileSync(COPILOT_INSTRUCTIONS_PATH, updated, 'utf8');
  console.log(`  Removed tscodesearch priority instruction from ${COPILOT_INSTRUCTIONS_PATH}.`);
}

// -- Argument parsing ----------------------------------------------------------

const argv      = process.argv.slice(2);
const uninstall = argv.includes('--uninstall');
const priority  = argv.includes('--priority');

// -- Uninstall -----------------------------------------------------------------

if (uninstall) {
  console.log('Stopping daemon...');
  run('node', [join(REPO, 'ts.mjs'), 'stop'], { stdio: 'pipe' });
  console.log('Removing codesearch MCP server...');
  if (run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch']) !== 0)
    console.log('WARNING: claude mcp remove failed (may not have been registered).');
  else
    console.log('  Removed from Claude Code.');
  try {
    updateCopilotMcp(COPILOT_MCP_PATH);
  } catch (e) {
    die(`Could not remove Copilot MCP registration: ${e.message}`);
  }
  // Remove from the dedicated VS Code / GitHub Copilot MCP configuration.
  const _mcpPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'mcp.json');
  try {
    const _mcp = JSON.parse(readFileSync(_mcpPath, 'utf8'));
    if (_mcp?.servers?.tscodesearch) {
      delete _mcp.servers.tscodesearch;
      writeFileSync(_mcpPath, JSON.stringify(_mcp, null, 2) + '\n', 'utf8');
      console.log('  Removed from VS Code MCP configuration.');
    }
  } catch { /* mcp.json missing or unreadable -- nothing to remove */ }
  // Clean up registrations created by older setup versions.
  const _vsPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'settings.json');
  try {
    const _s = JSON.parse(readFileSync(_vsPath, 'utf8'));
    if (_s?.mcp?.servers?.tscodesearch) {
      delete _s.mcp.servers.tscodesearch;
      if (Object.keys(_s.mcp.servers).length === 0) delete _s.mcp.servers;
      if (Object.keys(_s.mcp).length === 0) delete _s.mcp;
      writeFileSync(_vsPath, JSON.stringify(_s, null, 2) + '\n', 'utf8');
    }
  } catch { /* settings.json missing or unreadable -- nothing to remove */ }
  try {
    removePriorityInstruction();
  } catch (e) {
    console.log(`WARNING: Could not remove Copilot priority instruction: ${e.message}`);
  }
  console.log('Done. Reload VS Code / Claude Code for the change to take effect.');
  process.exit(0);
}

// -- Main setup ----------------------------------------------------------------

step(0, 'Checking uv');
if (!commandExists('uv')) {
  console.log('  uv not found -- installing via winget...');
  runOrDie('winget', ['install', '--id', 'astral-sh.uv', '-e', '--silent',
    '--accept-source-agreements', '--accept-package-agreements'], 'uv install');
  if (!commandExists('uv'))
    die('uv installed but not yet in PATH -- open a new terminal and re-run setup.');
}

// Stop the daemon before changing its Python environment. The stop command
// waits for the process lock to be released, not just for the HTTP port to close.
step(0, 'Stopping running daemon');
if (existsSync(join(REPO, 'config.json')))
  runOrDie('node', [join(REPO, 'ts.mjs'), 'stop'], 'ts stop');
else
  console.log('  No config.json yet; nothing to stop.');

step(1, 'Synchronizing managed Python environment (.venv)');
const pythonVersion = readFileSync(join(REPO, '.python-version'), 'utf8').trim();
runOrDie('uv', ['python', 'install', pythonVersion], 'uv python install');
removeIncompletePackageMetadata(join(REPO, '.venv', 'Lib', 'site-packages'));
runOrDie('uv', ['sync', '--locked', '--no-dev', '--link-mode', 'copy',
  '--managed-python', '--python', pythonVersion, '--project', REPO],
  'uv sync', { env: uvInvocation([]).env });
try {
  createDaemonExecutable();
} catch (e) {
  die(`Could not create named daemon executable: ${e.message}`);
}

// [1] Register MCP
step(2, 'Registering MCP server (Claude Code + GitHub Copilot)');
try {
  updateCopilotMcp(COPILOT_MCP_PATH, {
    command: 'uv',
    args: uvInvocation([join(REPO, 'mcp_server.py')]).args,
    env: { UV_PROJECT_ENVIRONMENT: join(REPO, '.venv') },
  });
  console.log(`  Registered with Copilot in ${COPILOT_MCP_PATH}.`);
} catch (e) {
  die(`Could not register Copilot MCP server: ${e.message}`);
}
run('claude', ['mcp', 'remove', '--scope', 'user', 'tscodesearch'], { stdio: 'pipe' });
if (run('claude', [
  'mcp', 'add', '--scope', 'user', 'tscodesearch',
  '--env', `UV_PROJECT_ENVIRONMENT=${join(REPO, '.venv')}`,
  '--', 'uv', ...uvInvocation([join(REPO, 'mcp_server.py')]).args,
]) === 0)
  console.log('  Registered with Claude Code.');
else
  console.log('  WARNING: claude mcp add failed -- Claude Code may not be installed.');

// Set the VS Code extension's repository path and remove legacy MCP settings.
const vscodeSettingsPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'settings.json');
try {
  let settings = {};
  if (existsSync(vscodeSettingsPath)) {
    settings = JSON.parse(readFileSync(vscodeSettingsPath, 'utf8'));
  }
  settings['tscodesearch.repoPath'] = REPO;
  if (settings?.mcp?.servers?.tscodesearch) {
    delete settings.mcp.servers.tscodesearch;
    if (Object.keys(settings.mcp.servers).length === 0) delete settings.mcp.servers;
    if (Object.keys(settings.mcp).length === 0) delete settings.mcp;
  }
  writeFileSync(vscodeSettingsPath, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  console.log(`  Set tscodesearch.repoPath to ${REPO}.`);
} catch (e) {
  console.log(`  WARNING: Could not update VS Code settings: ${e.message}`);
}

// Keep the VS Code Local harness registration for compatibility.
const vscodeMcpPath = join(process.env.APPDATA ?? '', 'Code', 'User', 'mcp.json');
if (commandExists('code')) {
  const mcpDefinition = JSON.stringify({
    name: 'tscodesearch',
    type: 'stdio',
    command: 'uv',
    args: uvInvocation([join(REPO, 'mcp_server.py')]).args,
    env: { UV_PROJECT_ENVIRONMENT: join(REPO, '.venv') },
  });
  if (run('code', ['--add-mcp', mcpDefinition]) === 0)
    console.log('  Registered with VS Code (GitHub Copilot).');
  else
    console.log('  WARNING: Could not update VS Code MCP configuration.');
} else {
  console.log("  WARNING: 'code' not found in PATH -- could not register with VS Code.");
  console.log(`  Add the tscodesearch server manually to ${vscodeMcpPath}.`);
}

if (priority) {
  try {
    addPriorityInstruction();
  } catch (e) {
    die(`Could not add Copilot priority instruction: ${e.message}`);
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
  runOrDie('npm', ['ci', '--no-fund', '--no-audit'], 'npm ci (vscode)', { cwd: vscodeDir });
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
