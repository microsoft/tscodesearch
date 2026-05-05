#!/usr/bin/env node
/**
 * run_tests.mjs — VS Code extension unit tests.
 *
 * Runs client.test.ts and pipeline.test.ts in vscode-codesearch/.
 * No Typesense server required — tests use a mock HTTP backend.
 *
 * Usage:
 *   node run_tests.mjs
 *
 * Python tests are run directly with pytest:
 *   ~/.local/indexserver-venv/bin/pytest tests/ query/tests/ -v
 * The tests/integration/conftest.py fixture handles Typesense startup
 * automatically when CODESEARCH_CONFIG is not already set.
 */

import { spawnSync }                            from 'node:child_process';
import { existsSync, writeFileSync, unlinkSync } from 'node:fs';
import { tmpdir }                               from 'node:os';
import { join, dirname }                        from 'node:path';
import { fileURLToPath }                        from 'node:url';

const REPO = dirname(fileURLToPath(import.meta.url));

function runCaptured(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, ...opts });
  return { status: r.error ? 1 : (r.status ?? 1), output: (r.stdout ?? '') + (r.stderr ?? '') };
}

function vscodeSummary(output) {
  const pass = output.match(/ℹ pass (\d+)/)?.[1];
  const fail = output.match(/ℹ fail (\d+)/)?.[1];
  return pass !== undefined ? `${pass} passed, ${fail ?? '0'} failed` : null;
}

const vscodeDir = join(REPO, 'vscode-codesearch');

if (!existsSync(join(vscodeDir, 'package.json'))) {
  console.log('[vscode] No vscode-codesearch/package.json found — skipping.');
  process.exit(0);
}

if (!existsSync(join(vscodeDir, 'node_modules'))) {
  console.log('[vscode] Installing npm dependencies...');
  const r = spawnSync('npm', ['ci', '--silent'], { cwd: vscodeDir, stdio: 'inherit' });
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

// Write a minimal config. VS Code unit tests use a mock HTTP backend so
// api_key and port only need to have valid structure.
const sampleRoot1 = join(REPO, 'sample', 'root1').replace(/\\/g, '/');
const sampleRoot2 = join(REPO, 'sample', 'root2').replace(/\\/g, '/');
const tmpCfg = join(tmpdir(), `vscode-test-config-${process.pid}.json`);
writeFileSync(tmpCfg, JSON.stringify({
  api_key: 'codesearch-test',
  port: 8108,
  roots: {
    root1: { path: sampleRoot1 },
    root2: { path: sampleRoot2 },
  },
}, null, 2));

let exitCode = 1;
try {
  const r = runCaptured('node', [
    '--require', 'tsx/cjs', '--test',
    'src/test/client.test.ts',
    'src/test/pipeline.test.ts',
  ], { cwd: vscodeDir, env: { ...process.env, CS_CONFIG: tmpCfg } });

  process.stdout.write(r.output);
  const summary = vscodeSummary(r.output);
  if (r.status === 0) {
    console.log(`[vscode] PASSED${summary ? `  (${summary})` : ''}`);
  } else {
    console.log(`[vscode] FAILED${summary ? `  (${summary})` : ''}`);
  }
  exitCode = r.status ?? 1;
} finally {
  if (existsSync(tmpCfg)) unlinkSync(tmpCfg);
}

process.exit(exitCode);
