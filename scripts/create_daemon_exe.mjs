import { spawnSync } from 'node:child_process';
import { copyFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { uvInvocation } from '../lib/uv.mjs';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));

export function createDaemonExecutable() {
  if (process.platform !== 'win32') return;
  const invocation = uvInvocation(['-c',
    'import json,sys; print(json.dumps({"base":sys._base_executable,"prefix":sys.prefix}))']);
  const result = spawnSync(invocation.command, invocation.args, {
    env: invocation.env, encoding: 'utf8', windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Could not inspect uv Python: ${result.stderr}`);
  const runtime = JSON.parse(result.stdout);
  if (resolve(runtime.prefix).toLowerCase() !== join(REPO, '.venv').toLowerCase()) {
    throw new Error('uv did not select the repository-root .venv.');
  }
  const base = dirname(runtime.base);
  const scripts = join(REPO, '.venv', 'Scripts');
  const files = [
    ['pythonw.exe', 'tscodesearch.exe'],
    ...readdirSync(base).filter(name => name.toLowerCase().endsWith('.dll')).map(name => [name, name]),
  ];
  for (const [sourceName, targetName] of files) {
    copyFileSync(join(base, sourceName), join(scripts, targetName));
  }
  const named = uvInvocation(['-c',
    'import json,sys; print(json.dumps({"prefix":sys.prefix}))'], process.env,
    join(scripts, 'tscodesearch.exe'));
  const check = spawnSync(named.command, named.args, {
    env: named.env, encoding: 'utf8', windowsHide: true,
  });
  if (check.error) throw check.error;
  if (check.status !== 0 || JSON.parse(check.stdout).prefix.toLowerCase() !== runtime.prefix.toLowerCase()) {
    throw new Error(`Named daemon executable did not activate .venv: ${check.stderr}`);
  }
  console.log(`Created ${join(scripts, 'tscodesearch.exe')} from uv-managed Python.`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    createDaemonExecutable();
  } catch (error) {
    console.error(`ERROR: Could not create tscodesearch.exe: ${error.message}`);
    process.exitCode = 1;
  }
}
