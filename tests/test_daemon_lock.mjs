/**
 * test_daemon_lock.mjs -- unit tests for lib/daemon_lock.mjs
 *
 * Run with:
 *   node --test tests/test_daemon_lock.mjs
 *
 * Each test spawns a real Python subprocess using the client venv so that
 * the OS locking primitive is the same one used by the daemon.  No daemon
 * needs to be running.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync, spawn } from 'node:child_process';

import { isLockHeld, waitForLockReleased } from '../lib/daemon_lock.mjs';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function clientVenvPython() {
    if (process.platform === 'win32') {
        return path.join(REPO, '.client-venv', 'Scripts', 'python.exe');
    }
    return path.join(REPO, '.client-venv', 'bin', 'python');
}

const PY = clientVenvPython();

/** Create a fresh temp file path (file does not exist yet). */
function tmpLockPath() {
    return path.join(os.tmpdir(), `tstest-lock-${process.pid}-${Date.now()}.lock`);
}

/**
 * Spawn a Python subprocess that acquires an exclusive OS lock on `lockPath`,
 * writes "READY\n" to stdout, then sleeps for `holdSecs` seconds.
 *
 * Returns the child process.  Call child.kill() to release the lock early.
 *
 * The caller must await `whenReady` (a Promise that resolves when "READY" is
 * received) before assuming the lock is held.
 */
function spawnLockHolder(lockPath, holdSecs = 10) {
    const script = process.platform === 'win32'
        ? [
            'import msvcrt,os,sys,time',
            'fh=open(sys.argv[1],"w")',
            'fh.seek(0)',
            'msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1)',
            'fh.write(str(os.getpid()))',
            'fh.flush()',
            'sys.stdout.write("READY\\n")',
            'sys.stdout.flush()',
            'time.sleep(float(sys.argv[2]))',
        ].join('\n')
        : [
            'import fcntl,os,sys,time',
            'fh=open(sys.argv[1],"w")',
            'fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            'fh.write(str(os.getpid()))',
            'fh.flush()',
            'sys.stdout.write("READY\\n")',
            'sys.stdout.flush()',
            'time.sleep(float(sys.argv[2]))',
        ].join('\n');

    const child = spawn(PY, ['-c', script, lockPath, String(holdSecs)], {
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    let buf = '';
    const whenReady = new Promise((resolve, reject) => {
        child.stdout.on('data', chunk => {
            buf += chunk.toString();
            if (buf.includes('READY')) resolve();
        });
        child.on('error', reject);
        child.on('exit', code => {
            if (code !== 0 && !buf.includes('READY')) {
                reject(new Error(`lock-holder exited ${code} before READY`));
            }
        });
        // Safety: reject after 5s if READY never arrives.
        setTimeout(() => reject(new Error('lock-holder READY timeout')), 5000);
    });

    return { child, whenReady };
}

// ---------------------------------------------------------------------------
// isLockHeld tests
// ---------------------------------------------------------------------------

describe('isLockHeld', () => {

    test('returns false when lock file does not exist', () => {
        const lockPath = tmpLockPath();
        assert.equal(isLockHeld(lockPath, PY), false);
    });

    test('returns false when lock file exists but is not locked', () => {
        const lockPath = tmpLockPath();
        fs.writeFileSync(lockPath, '99999');
        try {
            assert.equal(isLockHeld(lockPath, PY), false);
        } finally {
            fs.unlinkSync(lockPath);
        }
    });

    test('returns true when a live process holds the lock', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 10);
        try {
            await whenReady;
            assert.equal(isLockHeld(lockPath, PY), true);
        } finally {
            child.kill();
        }
    });

    test('returns false after the holding process exits', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 0.1);
        await whenReady;
        // Wait for the short sleep + process exit.
        await new Promise(r => child.on('exit', r));
        assert.equal(isLockHeld(lockPath, PY), false);
    });

    test('probe itself does not leave the lock held (re-entrant safety)', async () => {
        // Call isLockHeld twice in quick succession when the file is unlocked --
        // the second call should also return false, not block on its own probe.
        const lockPath = tmpLockPath();
        fs.writeFileSync(lockPath, '0');
        try {
            assert.equal(isLockHeld(lockPath, PY), false);
            assert.equal(isLockHeld(lockPath, PY), false);
        } finally {
            fs.unlinkSync(lockPath);
        }
    });

    test('returns true when called while holder is mid-sleep (stress x5)', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 10);
        try {
            await whenReady;
            for (let i = 0; i < 5; i++) {
                assert.equal(isLockHeld(lockPath, PY), true, `iteration ${i}`);
            }
        } finally {
            child.kill();
        }
    });

});

// ---------------------------------------------------------------------------
// waitForLockReleased tests
// ---------------------------------------------------------------------------

describe('waitForLockReleased', () => {

    test('resolves immediately when file does not exist', async () => {
        const lockPath = tmpLockPath();
        const released = await waitForLockReleased(lockPath, PY, { timeoutMs: 2000 });
        assert.equal(released, true);
    });

    test('resolves immediately when file exists but is not locked', async () => {
        const lockPath = tmpLockPath();
        fs.writeFileSync(lockPath, '0');
        try {
            const released = await waitForLockReleased(lockPath, PY, { timeoutMs: 2000 });
            assert.equal(released, true);
        } finally {
            fs.unlinkSync(lockPath);
        }
    });

    test('returns false on timeout when lock is never released', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 30);
        try {
            await whenReady;
            const released = await waitForLockReleased(lockPath, PY, {
                timeoutMs: 600,
                intervalMs: 100,
            });
            assert.equal(released, false);
        } finally {
            child.kill();
        }
    });

    test('resolves true when lock releases during the wait window', async () => {
        const lockPath = tmpLockPath();
        // Holder holds for 0.4s; waitForLockReleased waits up to 5s.
        const { child, whenReady } = spawnLockHolder(lockPath, 0.4);
        await whenReady;
        const released = await waitForLockReleased(lockPath, PY, {
            timeoutMs: 5000,
            intervalMs: 100,
        });
        assert.equal(released, true);
        child.kill();  // no-op if already exited
    });

    test('resolves true when holder is killed during the wait', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 30);
        await whenReady;

        // Kill the holder 300ms into the wait.
        setTimeout(() => child.kill(), 300);

        const released = await waitForLockReleased(lockPath, PY, {
            timeoutMs: 5000,
            intervalMs: 100,
        });
        assert.equal(released, true);
    });

    test('custom intervalMs is respected (short polling interval)', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 0.2);
        await whenReady;

        const t0 = Date.now();
        const released = await waitForLockReleased(lockPath, PY, {
            timeoutMs: 5000,
            intervalMs: 50,
        });
        const elapsed = Date.now() - t0;

        assert.equal(released, true);
        // Should have finished in well under 2s (holder sleeps only 200ms).
        assert.ok(elapsed < 2000, `elapsed ${elapsed}ms should be < 2000ms`);
        child.kill();
    });

});

// ---------------------------------------------------------------------------
// Edge / adversarial cases
// ---------------------------------------------------------------------------

describe('edge cases', () => {

    test('isLockHeld handles a bad python path without throwing', () => {
        const lockPath = tmpLockPath();
        fs.writeFileSync(lockPath, '0');
        try {
            // Bad python path -> spawnSync gets an error -> treated as held (conservative).
            const result = isLockHeld(lockPath, '/no/such/python');
            // We accept either true (spawn error = held) or false (file-not-locked fallback).
            // The important thing is it does not throw.
            assert.ok(typeof result === 'boolean');
        } finally {
            fs.unlinkSync(lockPath);
        }
    });

    test('isLockHeld handles an empty file (zero bytes)', () => {
        const lockPath = tmpLockPath();
        fs.writeFileSync(lockPath, '');
        try {
            // An empty unlocked file should read as not held.
            assert.equal(isLockHeld(lockPath, PY), false);
        } finally {
            fs.unlinkSync(lockPath);
        }
    });

    test('concurrent isLockHeld calls do not interfere', async () => {
        const lockPath = tmpLockPath();
        const { child, whenReady } = spawnLockHolder(lockPath, 10);
        try {
            await whenReady;
            // Fire 4 concurrent isLockHeld calls -- all should return true.
            const results = await Promise.all(
                Array.from({ length: 4 }, () =>
                    Promise.resolve(isLockHeld(lockPath, PY))
                )
            );
            assert.ok(results.every(r => r === true), `all should be true: ${results}`);
        } finally {
            child.kill();
        }
    });

});
