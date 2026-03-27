#!/usr/bin/env node
/**
 * run_tests.mjs — test runner for codesearch.
 *
 * Optimised for agent invocation: all subprocess output goes to log files;
 * only one status line per stage is printed to stdout. On failure the log
 * file path is emitted so the agent can read it directly.
 *
 * Modes
 * ──────
 *   --docker        Build image (if needed), spin up a container with sample
 *                   roots pre-indexed, run pytest + VS Code tests, tear down.
 *   --wsl           Run pytest in WSL via wsl.exe.  Non-destructive — starts a
 *                   fresh isolated Typesense on CODESEARCH_TEST_PORT (default 18108)
 *                   using a temp config; never touches the production instance.
 *   --linux         Run pytest directly (CI / native Linux).
 *
 * Flags
 * ──────
 *   --vscode        Force VS Code tests on  (default: on for all modes)
 *   --no-vscode     Skip VS Code tests in all modes.
 *   --print-logs    Print log file contents to stderr on failure (used by CI)
 *
 * Examples
 * ────────
 *   node run_tests.mjs --docker
 *   node run_tests.mjs --wsl
 *   node run_tests.mjs --wsl -k TestVerifier
 *   node run_tests.mjs --linux tests/test_indexer.py
 *
 * Environment overrides (wsl / linux modes)
 *   CODESEARCH_TEST_PORT  WSL test Typesense port  (default: 18108)
 *   CODESEARCH_CONFIG     Override config.json path (auto-set by --wsl)
 *   CODESEARCH_PORT       linux mode: Typesense port (default: from config.json)
 *   CODESEARCH_KEY        linux mode: API key (default: from config.json)
 *   TYPESENSE_VERSION     default: 27.1
 *   PYTEST                default: ~/.local/indexserver-venv/bin/pytest
 */

import { spawnSync }                      from 'node:child_process';
import { existsSync, writeFileSync,
         readFileSync, unlinkSync,
         mkdirSync, rmSync }              from 'node:fs';
import { tmpdir }                         from 'node:os';
import { join, dirname }                  from 'node:path';
import { fileURLToPath }                  from 'node:url';
import http                               from 'node:http';

const REPO = dirname(fileURLToPath(import.meta.url));

// ── Path helpers ──────────────────────────────────────────────────────────────

const toDockerPath = p => p.replace(/\\/g, '/');
const toWslPath    = p =>
  p.replace(/\\/g, '/').replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`);

// ── Output helpers ────────────────────────────────────────────────────────────

/** Create (or clear) the fixed log directory and print its path. */
function mkLogDir() {
  const dir = join(tmpdir(), 'codesearch-logs');
  try { rmSync(dir, { recursive: true, force: true }); } catch {}
  mkdirSync(dir, { recursive: true });
  console.log(`[run] logs → ${dir}`);
  return dir;
}

/** Print one status line.  Returns helpers to mark it done. */
function step(label) {
  process.stdout.write(`[${label}] `);
  return {
    ok(detail = '')   { console.log('OK' + (detail ? `  (${detail})` : '')); },
    fail(logPath, detail = '') {
      console.log('FAILED' + (detail ? `  (${detail})` : '') + `\n  → ${logPath}`);
    },
  };
}

/** Run a command, capture all output, return { status, output }. */
function runCaptured(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, {
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, ...opts,
  });
  return {
    status: r.error ? 1 : (r.status ?? 1),
    output: (r.stdout ?? '') + (r.stderr ?? ''),
  };
}

/** Run a command with stdio inherited (for interactive / cleanup use). */
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (r.error) throw r.error;
  return r.status ?? 1;
}

function runOrDie(cmd, args, opts = {}) {
  if (run(cmd, args, opts) !== 0) process.exit(1);
}

function capture(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', ...opts });
  if (r.error || r.status !== 0) return null;
  return r.stdout.trim();
}

// ── Pytest log splitting ───────────────────────────────────────────────────────

/**
 * Split raw pytest output into passed-only and error-only sections.
 * Shared lines (header, warnings, final summary) appear in both files.
 */
function splitPytestOutput(output) {
  const lines = output.split('\n');
  const passedLines = [];
  const errorLines = [];
  let inFailSection = false;

  for (const line of lines) {
    if (/^=+ (FAILURES|ERRORS) =+/.test(line)) {
      inFailSection = true;
      errorLines.push(line);
      continue;
    }
    if (inFailSection && /^=+ /.test(line)) {
      inFailSection = false;
    }
    if (inFailSection) {
      errorLines.push(line);
    } else if (/\sFAILED(\s|$)|^FAILED /.test(line) || /\sERROR(\s|$)|^ERROR /.test(line)) {
      errorLines.push(line);
    } else if (/\sPASSED(\s|$)/.test(line)) {
      passedLines.push(line);
    } else {
      // Shared: header, config, warnings summary, final stats line
      passedLines.push(line);
      errorLines.push(line);
    }
  }

  return { passed: passedLines.join('\n'), errors: errorLines.join('\n') };
}

/** Write split pytest logs; return { passedLog, errorsLog }. */
function writePytestLogs(logDir, output) {
  const passedLog = join(logDir, 'pytest_passed.log');
  const errorsLog = join(logDir, 'pytest_errors.log');
  const { passed, errors } = splitPytestOutput(output);
  writeFileSync(passedLog, passed);
  writeFileSync(errorsLog, errors);
  return { passedLog, errorsLog };
}

// ── Summaries extracted from captured output ──────────────────────────────────

function pytestSummary(output) {
  // Take the last match — the final "N passed, M failed in X.Xs" line.
  const matches = [...output.matchAll(/=+ ([\d]+ \w+[^=\n]*? in [\d.]+s) =+/g)];
  if (matches.length === 0) return null;
  return matches.at(-1)[1].trim();
}

/** Extract the first few FAILED/ERROR test names from the short summary section. */
function pytestDetail(output, limit = 8) {
  const lines = output.split('\n');
  const start = lines.findIndex(l => l.includes('short test summary info'));
  if (start === -1) return null;
  const results = [];
  for (let i = start + 1; i < lines.length && results.length < limit; i++) {
    const l = lines[i];
    if (l.startsWith('FAILED ') || l.startsWith('ERROR ')) {
      const m = l.match(/^(FAILED|ERROR)\s+(\S+)/);
      if (m) results.push(`    ${m[1]} ${m[2]}`);
    } else if (l.startsWith('===')) break;
  }
  return results.length > 0 ? results.join('\n') : null;
}

function vscodeSummary(output) {
  const pass = output.match(/ℹ pass (\d+)/)?.[1];
  const fail = output.match(/ℹ fail (\d+)/)?.[1];
  return pass !== undefined ? `${pass} passed, ${fail ?? '0'} failed` : null;
}

// ── HTTP health check ─────────────────────────────────────────────────────────

function httpHealth(port) {
  return new Promise(resolve => {
    const req = http.request(
      { hostname: 'localhost', port, path: '/health', timeout: 3000 },
      res => {
        let body = '';
        res.on('data', d => body += d);
        res.on('end', () => {
          try { resolve(JSON.parse(body).ok === true); }
          catch { resolve(false); }
        });
      }
    );
    req.on('error',   () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function die(msg) { console.error(`ERROR: ${msg}`); process.exit(1); }

// ── Read config.json ──────────────────────────────────────────────────────────

function readConfig() {
  const cfgPath = join(REPO, 'config.json');
  if (!existsSync(cfgPath)) return {};
  try { return JSON.parse(readFileSync(cfgPath, 'utf8')); } catch { return {}; }
}

// ── Argument parsing ──────────────────────────────────────────────────────────

let mode       = null;
let runVscode  = 'auto';
let printLogs  = false;
const extraArgs = [];

for (const arg of process.argv.slice(2)) {
  if      (arg === '--docker')      mode      = 'docker';
  else if (arg === '--wsl')         mode      = 'wsl';
  else if (arg === '--linux')       mode      = 'linux';
  else if (arg === '--vscode')      runVscode = 'true';
  else if (arg === '--no-vscode')   runVscode = 'false';
  else if (arg === '--print-logs')  printLogs = true;
  else                              extraArgs.push(arg);
}

if (!mode) die('No mode specified. Use --docker, --wsl, or --linux.');

// ── Dispatch ──────────────────────────────────────────────────────────────────

if      (mode === 'docker') await runDocker();
else if (mode === 'wsl')    await runWsl();
else                        await runLinux();

// =============================================================================
// DOCKER MODE
// =============================================================================

async function runDocker() {
  const IMAGE       = 'codesearch-mcp';
  const CONTAINER   = `codesearch-e2e-${process.pid}`;
  const DATA_VOL    = `codesearch_e2e_data_${process.pid}`;
  const API_PORT    = 18109;
  const API_KEY     = 'e2e-test-key';
  const logDir      = mkLogDir();
  console.log(`[run] pytest passed → ${join(logDir, 'pytest_passed.log')}`);
  console.log(`[run] pytest errors → ${join(logDir, 'pytest_errors.log')}`);

  const sampleRoot1 = join(REPO, 'sample', 'root1');
  const sampleRoot2 = join(REPO, 'sample', 'root2');

  if (!existsSync(sampleRoot1)) die(`sample/root1 not found at ${sampleRoot1}`);
  if (!existsSync(sampleRoot2)) die(`sample/root2 not found at ${sampleRoot2}`);

  if (runCaptured('docker', ['info', '--format', '{{.ID}}']).status !== 0)
    die('Docker is not running. Start Docker Desktop first.');

  // Always rebuild image to pick up latest code changes
  {
    const s = step('docker/build');
    const buildLog = join(logDir, 'build.log');
    const r = runCaptured('docker', [
      'build', '-t', IMAGE,
      '-f', toDockerPath(join(REPO, 'docker', 'Dockerfile')),
      toDockerPath(REPO),
    ]);
    writeFileSync(buildLog, r.output);
    if (r.status !== 0) { s.fail(buildLog); process.exit(r.status); }
    s.ok();
  }

  // Temp config for both sample roots
  const tmpConfig = join(tmpdir(), `e2e-config-${process.pid}.json`);
  writeFileSync(tmpConfig, JSON.stringify({
    api_key: API_KEY, port: 8108,
    roots: {
      root1:  { local_path: '/app/sample/root1' },
      root2:  { local_path: '/app/sample/root2' },
      sample: { local_path: '/app/sample/root1' },
    },
  }, null, 2));

  let cleanedUp = false;
  function cleanup() {
    if (cleanedUp) return;
    cleanedUp = true;
    run('docker', ['stop',         CONTAINER], { stdio: 'pipe' });
    run('docker', ['rm',           CONTAINER], { stdio: 'pipe' });
    run('docker', ['volume', 'rm', DATA_VOL],  { stdio: 'pipe' });
    if (existsSync(tmpConfig)) unlinkSync(tmpConfig);
  }
  process.on('exit',   cleanup);
  process.on('SIGINT',  () => process.exit(130));
  process.on('SIGTERM', () => process.exit(143));

  // Start container
  {
    const s = step('docker/start');
    const testsDir = join(REPO, 'tests');
    const r = runCaptured('docker', [
      'run', '-d', '--name', CONTAINER,
      '-p', `127.0.0.1:${API_PORT}:8109`,
      '-e', 'CODESEARCH_API_HOST=0.0.0.0',
      '-v', `${toDockerPath(sampleRoot1)}:/app/sample/root1:ro`,
      '-v', `${toDockerPath(sampleRoot2)}:/app/sample/root2:ro`,
      '-v', `${toDockerPath(testsDir)}:/app/tests:ro`,
      '-v', `${toDockerPath(tmpConfig)}:/app/config.json`,
      '-v', `${DATA_VOL}:/typesensedata`,
      IMAGE,
    ]);
    if (r.status !== 0) {
      const log = join(logDir, 'start.log');
      writeFileSync(log, r.output);
      s.fail(log); process.exit(r.status);
    }
    s.ok(CONTAINER);
  }

  // Wait for management API to report ready (entrypoint health check)
  {
    const s = step('docker/ready');
    const startMs = Date.now();
    const timeoutMs = 90_000;
    let ready = false;
    while (Date.now() - startMs < timeoutMs) {
      if (await httpHealth(API_PORT)) { ready = true; break; }
      await sleep(1000);
    }
    if (!ready) {
      const log = join(logDir, 'startup.log');
      saveContainerLogs(CONTAINER, logDir);
      s.fail(log, `management API on port ${API_PORT} did not become healthy within 90s`);
      process.exit(1);
    }
    s.ok(`${Math.round((Date.now() - startMs) / 1000)}s`);
  }

  // Run e2e suite (waits for health + collections, then pytest)
  {
    const s = step('docker/pytest');
    const r = runCaptured('docker', [
      'exec', CONTAINER,
      '/app/scripts/e2e.sh', 'run-suite',
      'codesearch_root1', 'codesearch_root2', 'codesearch_sample',
      '--', ...extraArgs,
    ]);
    const { passedLog, errorsLog } = writePytestLogs(logDir, r.output);
    if (r.status !== 0) {
      s.fail(errorsLog, pytestSummary(r.output));
      const d = pytestDetail(r.output); if (d) console.log(d);
      if (printLogs) console.error('\n--- pytest_errors.log ---\n' +
        readFileSync(errorsLog, 'utf8') + '\n--- end pytest_errors.log ---');
      saveContainerLogs(CONTAINER, logDir);
      process.exit(r.status);
    }
    s.ok(pytestSummary(r.output));
  }

  // VS Code extension tests (on by default in Docker mode)
  if (runVscode !== 'false') {
    const vscodeLog = join(logDir, 'vscode.log');
    const s = step('docker/vscode');
    const status = await runVscodeTests({ apiPort: API_PORT, apiKey: API_KEY,
                                          logFile: vscodeLog, container: CONTAINER,
                                          logDir,
                                          roots: {
                                            root1:  { external_path: '/app/sample/root1' },
                                            root2:  { external_path: '/app/sample/root2' },
                                            sample: { external_path: '/app/sample/root1' },
                                          } });
    if (status !== 0) {
      const logContent = readFileSync(vscodeLog, 'utf8');
      s.fail(vscodeLog, vscodeSummary(logContent));
      if (printLogs) console.error('\n--- vscode.log ---\n' + logContent + '\n--- end vscode.log ---');
      process.exit(status);
    }
    s.ok(vscodeSummary(readFileSync(vscodeLog, 'utf8')));
  }

  console.log('[docker] PASSED');
}

// =============================================================================
// WSL MODE
// =============================================================================

async function runWsl() {
  // Use a dedicated test port so the test instance never touches the production
  // Typesense.  CODESEARCH_TEST_PORT overrides the default.
  const TEST_PORT = parseInt(process.env.CODESEARCH_TEST_PORT ?? 18108, 10);
  const TEST_KEY  = 'codesearch-test';
  const TYPESENSE_VERSION = process.env.TYPESENSE_VERSION ?? '27.1';
  const wslRepo = toWslPath(REPO);
  const PYTEST  = (process.env.PYTEST ?? '~/.local/indexserver-venv/bin/pytest')
                    .replace(/^~/, '$HOME');

  const DATA_DIR    = '/tmp/codesearch-wsl-test';
  const TS_DIR      = '/tmp/codesearch-wsl-test-ts';
  const CONFIG_FILE = '/tmp/codesearch-wsl-test-config.json';
  const logDir      = mkLogDir();

  // Write isolated test config with sample roots so the management API indexes
  // them during startup (completed well before pytest finishes ~25 s).
  const wslRoot1 = `${wslRepo}/sample/root1`;
  const wslRoot2 = `${wslRepo}/sample/root2`;
  const winRoot1 = join(REPO, 'sample', 'root1').replace(/\\/g, '/');
  const winRoot2 = join(REPO, 'sample', 'root2').replace(/\\/g, '/');
  const testConfig = JSON.stringify({
    api_key: TEST_KEY, port: TEST_PORT,
    roots: {
      root1:  { local_path: wslRoot1, external_path: winRoot1 },
      root2:  { local_path: wslRoot2, external_path: winRoot2 },
      sample: { local_path: wslRoot1, external_path: winRoot1 },
    },
  }, null, 2);
  {
    const r = spawnSync('wsl.exe', ['-e', 'bash', '-c', `cat > '${CONFIG_FILE}'`],
      { input: testConfig, encoding: 'utf8' });
    if (r.status !== 0) die('Failed to write test config to WSL.');
  }

  const testTargets = extraArgs.length > 0 ? extraArgs : ['tests/'];
  const quoted = testTargets.map(a => `'${a.replace(/'/g, "'\\''")}'`).join(' ');

  // Announce log paths before running so they're visible even if the run crashes.
  console.log(`[run] pytest passed → ${join(logDir, 'pytest_passed.log')}`);
  console.log(`[run] pytest errors → ${join(logDir, 'pytest_errors.log')}`);

  const r = runCaptured('wsl.exe', ['-e', 'bash', '-lc',
    `TYPESENSE_VERSION='${TYPESENSE_VERSION}' ` +
    `TYPESENSE_DATA='${DATA_DIR}' TYPESENSE_DIR='${TS_DIR}' ` +
    `CONFIG_FILE='${CONFIG_FILE}' CODESEARCH_PORT=${TEST_PORT} ` +
    `CODESEARCH_CONFIG='${CONFIG_FILE}' ` +
    `APP_ROOT='${wslRepo}' PYTEST="${PYTEST}" ` +
    `bash '${wslRepo}/scripts/run-tests-with-server.sh' "${PYTEST}" -v ${quoted}`,
  ]);
  writeFileSync(join(logDir, 'all.log'), r.output);

  const { passedLog, errorsLog } = writePytestLogs(logDir, r.output);

  {
    const s = step('wsl/pytest');
    if (pytestSummary(r.output)?.includes('failed') || r.status !== 0) {
      s.fail(errorsLog, pytestSummary(r.output));
      const d = pytestDetail(r.output); if (d) console.log(d);
      if (printLogs) console.error('\n--- pytest_errors.log ---\n' +
        readFileSync(errorsLog, 'utf8') + '\n--- end pytest_errors.log ---');
      process.exit(r.status || 1);
    }
    s.ok(pytestSummary(r.output));
  }

  if (runVscode !== 'false') {
    const vscodeLog = join(logDir, 'vscode.log');
    const s = step('wsl/vscode');
    const status = await runVscodeTests({
      apiPort: TEST_PORT + 1, apiKey: TEST_KEY, logFile: vscodeLog,
      roots: {
        root1: { external_path: winRoot1 },
        root2: { external_path: winRoot2 },
      },
    });
    if (status !== 0) {
      const logContent = readFileSync(vscodeLog, 'utf8');
      s.fail(vscodeLog, vscodeSummary(logContent));
      if (printLogs) console.error('\n--- vscode.log ---\n' + logContent + '\n--- end vscode.log ---');
      process.exit(status || 1);
    }
    s.ok(vscodeSummary(readFileSync(vscodeLog, 'utf8')));
  }

  console.log('[wsl] PASSED');
  process.exit(0);
}

// =============================================================================
// LINUX MODE
// =============================================================================

async function runLinux() {
  const TEST_PORT         = parseInt(process.env.CODESEARCH_TEST_PORT ?? 18108, 10);
  const TEST_KEY          = 'codesearch-test';
  const PYTEST            = process.env.PYTEST ?? `${process.env.HOME}/.local/indexserver-venv/bin/pytest`;
  const TYPESENSE_VERSION = process.env.TYPESENSE_VERSION ?? '27.1';
  const DATA_DIR          = '/tmp/codesearch-linux-test';
  const TS_DIR            = '/tmp/codesearch-linux-test-ts';
  const CONFIG_FILE       = '/tmp/codesearch-linux-test-config.json';
  const logDir            = mkLogDir();

  if (!existsSync(PYTEST)) die(`pytest not found at ${PYTEST}\nRun setup first.`);

  const sampleRoot1 = join(REPO, 'sample', 'root1');
  const sampleRoot2 = join(REPO, 'sample', 'root2');

  writeFileSync(CONFIG_FILE, JSON.stringify({
    api_key: TEST_KEY, port: TEST_PORT,
    roots: {
      root1: { local_path: sampleRoot1, external_path: sampleRoot1 },
      root2: { local_path: sampleRoot2, external_path: sampleRoot2 },
    },
  }, null, 2));

  process.on('SIGINT',  () => process.exit(130));
  process.on('SIGTERM', () => process.exit(143));

  const testTargets = extraArgs.length > 0 ? extraArgs : ['tests/'];

  console.log(`[run] pytest passed → ${join(logDir, 'pytest_passed.log')}`);
  console.log(`[run] pytest errors → ${join(logDir, 'pytest_errors.log')}`);

  const r = runCaptured('bash', [
    join(REPO, 'scripts', 'run-tests-with-server.sh'), PYTEST, '-v', ...testTargets,
  ], {
    env: {
      ...process.env,
      TYPESENSE_VERSION,
      TYPESENSE_DATA: DATA_DIR,
      TYPESENSE_DIR:  TS_DIR,
      CONFIG_FILE,
      CODESEARCH_PORT:   String(TEST_PORT),
      CODESEARCH_CONFIG: CONFIG_FILE,
      APP_ROOT: REPO,
      PYTEST,
    },
  });
  writeFileSync(join(logDir, 'all.log'), r.output);

  const { passedLog, errorsLog } = writePytestLogs(logDir, r.output);

  {
    const s = step('linux/pytest');
    if (pytestSummary(r.output)?.includes('failed') || r.status !== 0) {
      s.fail(errorsLog, pytestSummary(r.output));
      const d = pytestDetail(r.output); if (d) console.log(d);
      if (printLogs) console.error('\n--- pytest_errors.log ---\n' +
        readFileSync(errorsLog, 'utf8') + '\n--- end pytest_errors.log ---');
      process.exit(r.status || 1);
    }
    s.ok(pytestSummary(r.output));
  }

  if (runVscode !== 'false') {
    const vscodeLog = join(logDir, 'vscode.log');
    const s = step('linux/vscode');
    const status = await runVscodeTests({
      apiPort: TEST_PORT + 1, apiKey: TEST_KEY, logFile: vscodeLog,
      roots: {
        root1: { external_path: sampleRoot1 },
        root2: { external_path: sampleRoot2 },
      },
    });
    if (status !== 0) {
      const logContent = readFileSync(vscodeLog, 'utf8');
      s.fail(vscodeLog, vscodeSummary(logContent));
      if (printLogs) console.error('\n--- vscode.log ---\n' + logContent + '\n--- end vscode.log ---');
      process.exit(status || 1);
    }
    s.ok(vscodeSummary(readFileSync(vscodeLog, 'utf8')));
  }

  console.log('[linux] PASSED');
  process.exit(0);
}

// =============================================================================
// Save container logs on failure
// =============================================================================

function saveContainerLogs(container, logDir) {
  const containerLog = join(logDir, 'container.log');
  const crashLog     = join(logDir, 'api_crash.log');

  const logsResult = spawnSync('docker', ['logs', container], { encoding: 'utf8' });
  writeFileSync(containerLog, (logsResult.stdout ?? '') + (logsResult.stderr ?? ''));
  console.log(`  container.log → ${containerLog}`);

  const crashResult = spawnSync('docker', [
    'exec', container, 'cat', '/typesensedata/api_crash.log',
  ], { encoding: 'utf8' });
  if (crashResult.status === 0 && crashResult.stdout.trim()) {
    writeFileSync(crashLog, crashResult.stdout);
    console.log(`  api_crash.log → ${crashLog}`);
  }
}

// =============================================================================
// VS Code extension tests
// =============================================================================

async function runVscodeTests({ apiPort, apiKey, logFile, container = null, logDir = null, roots = {} }) {
  const vscodeDir = join(REPO, 'vscode-codesearch');
  if (!existsSync(join(vscodeDir, 'package.json'))) return 0;

  if (!existsSync(join(vscodeDir, 'node_modules'))) {
    runOrDie('npm', ['ci', '--silent'], { cwd: vscodeDir, stdio: 'pipe' });
  }

  const tmpCfg = join(tmpdir(), `e2e-ext-config-${process.pid}.json`);
  writeFileSync(tmpCfg, JSON.stringify(
    { api_key: apiKey, port: apiPort - 1, roots }, null, 2));

  const r = runCaptured('node', [
    '--require', 'tsx/cjs', '--test',
    'src/test/client.test.ts',
    'src/test/pipeline.test.ts',
  ], {
    cwd: vscodeDir,
    env: { ...process.env, CS_CONFIG: tmpCfg },
  });

  if (existsSync(tmpCfg)) unlinkSync(tmpCfg);
  writeFileSync(logFile, r.output);

  if (r.status !== 0 && container && logDir) {
    saveContainerLogs(container, logDir);
  }

  return r.status;
}
