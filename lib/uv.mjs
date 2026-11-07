import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));

export function uvInvocation(args, env = process.env, executable = 'python') {
  return {
    command: 'uv',
    args: ['run', '--no-sync', '--project', REPO, '--directory', REPO, executable, ...args],
    env: { ...env, UV_PROJECT_ENVIRONMENT: join(REPO, '.venv') },
  };
}

export function daemonUvInvocation(env = process.env) {
  const executable = process.platform === 'win32'
    ? join(REPO, '.venv', 'Scripts', 'tscodesearch.exe') : 'python';
  return uvInvocation(['-m', 'indexserver.daemon'], env, executable);
}
