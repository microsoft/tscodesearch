/**
 * daemon_lock.mjs -- helpers for probing the daemon OS-level file lock.
 *
 * The Python daemon holds an exclusive OS lock on daemon.lock for its entire
 * lifetime (acquired in _try_acquire_lock(), never explicitly released).
 * The OS releases the lock the moment the process truly dies -- clean, killed,
 * or crashed.
 *
 * isLockHeld() lets Node callers ask "is the daemon process still alive?"
 * without trusting the port (which closes mid-shutdown) or the PID file
 * (which is written at startup, not on exit).
 *
 * Locking mechanism mirrors daemon.py exactly:
 *   Windows -- msvcrt.locking() byte-range lock on byte 0
 *   Unix    -- fcntl.flock() exclusive advisory lock
 *
 * Both are checked by spawning a tiny Python snippet so that the same OS
 * primitive is used on both sides of the check.
 */

import fs           from 'node:fs';
import { spawnSync } from 'node:child_process';

// -- Lock probe scripts (ASCII only, no Unicode) --------------------------------

// Each script:
//   * opens the lock file for writing (r+b / r+)
//   * attempts a non-blocking exclusive lock
//   * exits 0 if the lock was free (acquired + released)
//   * exits 1 if the lock is held by another process (or any error)
//
// Using try/except with bare `except:` ensures any unexpected error (file
// missing, permission denied, bad path) also returns 1 = "treat as held"
// which is the safe direction: we will wait or re-check rather than blindly
// proceeding.

const _PROBE_WIN = [
    'import msvcrt,sys',
    'fh=open(sys.argv[1],"r+b");fh.seek(0);msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1);fh.close()',
].join('\n');

const _PROBE_UNIX = [
    'import fcntl,sys',
    'fh=open(sys.argv[1],"r+");fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB);fh.close()',
].join('\n');

const _PROBE_SCRIPT = process.platform === 'win32' ? _PROBE_WIN : _PROBE_UNIX;

// -- Public API ----------------------------------------------------------------

/**
 * Synchronously check whether daemon.lock is held by a live process.
 *
 * @param {string} lockPath   Absolute path to daemon.lock.
 * @param {string} pythonExe  Path to the Python interpreter to use for the probe.
 * @returns {boolean}         true = lock is held (process alive)
 *                            false = lock is free (process dead or file absent)
 */
export function isLockHeld(lockPath, pythonExe) {
    if (!fs.existsSync(lockPath)) return false;

    const result = spawnSync(
        pythonExe,
        ['-c', _PROBE_SCRIPT, lockPath],
        { timeout: 3000, stdio: 'pipe' },
    );

    // status 0  = probe acquired the lock => it was free => no daemon alive
    // status !== 0 = probe failed to acquire => lock is held => daemon alive
    //              (also covers timeout / spawn error -- treat as held = safe)
    if (result.error) return true;  // spawn failed -- conservative: treat as held
    return result.status !== 0;
}

/**
 * Poll until daemon.lock is released or the timeout elapses.
 *
 * @param {string}  lockPath    Absolute path to daemon.lock.
 * @param {string}  pythonExe   Python interpreter for the probe.
 * @param {object}  [opts]
 * @param {number}  [opts.timeoutMs=10000]   Max wait in milliseconds.
 * @param {number}  [opts.intervalMs=300]    Poll interval in milliseconds.
 * @returns {Promise<boolean>}  true = released within timeout, false = timed out.
 */
export async function waitForLockReleased(lockPath, pythonExe, {
    timeoutMs = 10_000,
    intervalMs = 300,
} = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (!isLockHeld(lockPath, pythonExe)) return true;
        await new Promise(r => setTimeout(r, intervalMs));
    }
    // One final check: the last poll might have been just before release.
    return !isLockHeld(lockPath, pythonExe);
}
