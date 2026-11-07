import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { readFileSync, existsSync } from 'node:fs';
import { uvInvocation, daemonUvInvocation } from '../lib/uv.mjs';

const repo = dirname(dirname(fileURLToPath(import.meta.url)));

test('MCP workspace registrations launch uv directly, not a Python wrapper', () => {
  const portable = JSON.parse(readFileSync(join(repo, '.mcp.json'), 'utf8')).mcpServers.tscodesearch;
  const vscode = JSON.parse(readFileSync(join(repo, 'vscode-codesearch', '.vscode', 'mcp.json'), 'utf8')).servers.tscodesearch;
  for (const definition of [portable, vscode]) {
    assert.equal(definition.command, 'uv');
    assert.equal(definition.args[0], 'run');
    assert.ok(definition.args.includes('--no-sync'));
    assert.ok(definition.args.includes('python'));
    assert.ok(definition.env.UV_PROJECT_ENVIRONMENT.endsWith('.venv'));
  }
  assert.equal(existsSync(join(repo, 'python.mjs')), false);
});

test('uv invocation pins the project and environment without runtime sync', () => {
  const invocation = uvInvocation(['-m', 'query'], { WIDGET: 'kept', UV_PROJECT_ENVIRONMENT: 'other' });
  assert.equal(invocation.command, 'uv');
  assert.deepEqual(invocation.args, [
    'run', '--no-sync', '--project', repo, '--directory', repo, 'python', '-m', 'query',
  ]);
  assert.equal(invocation.env.WIDGET, 'kept');
  assert.equal(invocation.env.UV_PROJECT_ENVIRONMENT, join(repo, '.venv'));
});

test('direct uv works outside the repository and keeps stdout clean', () => {
  const invocation = uvInvocation(['-c',
    'import json,os,sys,mcp; print(json.dumps({"cwd":os.getcwd(),"prefix":sys.prefix,"arg":sys.argv[1]}))',
    'Widget with spaces']);
  const result = spawnSync(invocation.command, invocation.args, {
    env: invocation.env, cwd: tmpdir(), encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.cwd.toLowerCase(), repo.toLowerCase());
  assert.equal(output.prefix.toLowerCase(), join(repo, '.venv').toLowerCase());
  assert.equal(output.arg, 'Widget with spaces');
});

test('Python failures propagate through direct uv', () => {
  const invocation = uvInvocation(['-c', 'raise SystemExit(7)']);
  const result = spawnSync(invocation.command, invocation.args, { env: invocation.env });
  assert.equal(result.status, 7);
});

test('daemon uv invocation selects the named executable only on Windows', () => {
  const invocation = daemonUvInvocation();
  assert.equal(invocation.args[6], process.platform === 'win32'
    ? join(repo, '.venv', 'Scripts', 'tscodesearch.exe') : 'python');
  assert.deepEqual(invocation.args.slice(7), ['-m', 'indexserver.daemon']);
});

test('named Windows daemon is the actual process image and uses root .venv', {
  skip: process.platform !== 'win32',
}, () => {
  const executable = join(repo, '.venv', 'Scripts', 'tscodesearch.exe');
  const invocation = uvInvocation(['-c', [
    'import ctypes,json,sys,tantivy,pystray,ssl',
    'image=ctypes.create_unicode_buffer(32768)',
    'assert ctypes.windll.kernel32.GetModuleFileNameW(None,image,len(image))',
    'print(json.dumps({"image":image.value,"prefix":sys.prefix}))',
  ].join('\n')], process.env, executable);
  const result = spawnSync(invocation.command, invocation.args, {
    env: invocation.env, encoding: 'utf8', windowsHide: true,
  });
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.image.toLowerCase(), executable.toLowerCase());
  assert.equal(output.prefix.toLowerCase(), join(repo, '.venv').toLowerCase());
});
