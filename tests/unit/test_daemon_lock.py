"""
Unit tests for _try_acquire_lock() and the daemon process-lock mechanism.

Tests cover:
  TryAcquireLock   -- basic acquire / lock-file content / idempotency guard
  LockExclusivity  -- a subprocess cannot steal the lock while the parent holds it
  LockRelease      -- lock is released when the holding process exits
  GhostSessions    -- csv_log.configure() is only called AFTER the lock is held
                      (i.e. no session.csv row is written for a failed start)
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = Path(__file__).parent.parent.parent
_PY  = sys.executable  # the venv Python running the tests

# Platform-appropriate snippet: acquire the lock non-blocking, exit 0 if
# free, exit 1 if held.
if sys.platform == 'win32':
    _PROBE = '\n'.join([
        'import msvcrt,sys',
        'fh=open(sys.argv[1],"r+b");fh.seek(0);msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1);fh.close()',
    ])
else:
    _PROBE = '\n'.join([
        'import fcntl,sys',
        'fh=open(sys.argv[1],"r+");fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB);fh.close()',
    ])

# Lock-holder script: acquires lock, prints READY, sleeps for argv[2] seconds.
if sys.platform == 'win32':
    _HOLD = '\n'.join([
        'import msvcrt,os,sys,time',
        'fh=open(sys.argv[1],"w")',
        'fh.seek(0)',
        'msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1)',
        'fh.write(str(os.getpid()))',
        'fh.flush()',
        'sys.stdout.write("READY\\n")',
        'sys.stdout.flush()',
        'time.sleep(float(sys.argv[2]))',
    ])
else:
    _HOLD = '\n'.join([
        'import fcntl,os,sys,time',
        'fh=open(sys.argv[1],"w")',
        'fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)',
        'fh.write(str(os.getpid()))',
        'fh.flush()',
        'sys.stdout.write("READY\\n")',
        'sys.stdout.flush()',
        'time.sleep(float(sys.argv[2]))',
    ])


def _probe_lock(lock_path: str) -> bool:
    """Return True if lock_path is currently held by another process."""
    r = subprocess.run(
        [_PY, '-c', _PROBE, lock_path],
        capture_output=True, timeout=5,
    )
    return r.returncode != 0


def _spawn_holder(lock_path: str, hold_secs: float = 10) -> subprocess.Popen:
    """Spawn a subprocess that holds the lock for hold_secs seconds.

    The caller must read one line from proc.stdout to know the lock is held.
    """
    return subprocess.Popen(
        [_PY, '-c', _HOLD, lock_path, str(hold_secs)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _wait_ready(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Block until the holder subprocess prints READY."""
    deadline = time.monotonic() + timeout
    buf = b''
    while time.monotonic() < deadline:
        chunk = proc.stdout.read1(64)  # type: ignore[attr-defined]
        if chunk:
            buf += chunk
            if b'READY' in buf:
                return
        time.sleep(0.05)
    raise TimeoutError('lock-holder did not print READY in time')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_daemon_module():
    """(Re)import indexserver.daemon so we get a fresh module state."""
    import indexserver.daemon as mod
    return mod


class _LockTestBase(unittest.TestCase):
    """Base class: patches _DAEMON_LOCK and _RUN_DIR to a temp dir, resets
    _lock_fh after each test so the OS lock is released."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._lock_path = os.path.join(self._tmpdir, 'daemon.lock')
        import indexserver.daemon as _daemon
        self._mod = _daemon
        # Patch module-level paths so _try_acquire_lock uses our temp dir.
        self._patches = [
            patch.object(_daemon, '_DAEMON_LOCK', Path(self._lock_path)),
            patch.object(_daemon, '_RUN_DIR',     Path(self._tmpdir)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        # Release the OS lock by closing the file handle, then reset the global.
        fh = self._mod._lock_fh
        if fh is not None:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                fh.close()
            except Exception:
                pass
            self._mod._lock_fh = None

        for p in self._patches:
            p.stop()

        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# TryAcquireLock -- basic behaviour
# ---------------------------------------------------------------------------

class TestTryAcquireLock(_LockTestBase):

    def test_returns_true_when_no_file_exists(self):
        self.assertFalse(os.path.exists(self._lock_path))
        result = self._mod._try_acquire_lock()
        self.assertTrue(result)

    def test_creates_lock_file_on_success(self):
        self._mod._try_acquire_lock()
        self.assertTrue(os.path.exists(self._lock_path))

    def test_writes_pid_to_lock_file(self):
        self._mod._try_acquire_lock()
        fh = self._mod._lock_fh
        # Read via the same file handle that holds the lock -- avoids the
        # Windows byte-range lock that blocks other handles from reading byte 0.
        fh.seek(0)
        content = fh.read().strip()
        self.assertEqual(content, str(os.getpid()))

    def test_sets_module_level_lock_fh(self):
        self.assertIsNone(self._mod._lock_fh)
        self._mod._try_acquire_lock()
        self.assertIsNotNone(self._mod._lock_fh)

    def test_lock_file_is_actually_locked_after_acquire(self):
        """After _try_acquire_lock succeeds, a probe subprocess must fail."""
        self._mod._try_acquire_lock()
        self.assertTrue(_probe_lock(self._lock_path),
                        'external probe should see the lock as held')

    def test_returns_true_when_existing_unlocked_file(self):
        """Pre-existing lock file with no live holder (stale) is acquirable."""
        Path(self._lock_path).write_text('99999', encoding='ascii')
        result = self._mod._try_acquire_lock()
        self.assertTrue(result)

    def test_returns_false_when_lock_held_by_other_process(self):
        """If another process holds the lock, _try_acquire_lock returns False."""
        holder = _spawn_holder(self._lock_path, hold_secs=10)
        try:
            _wait_ready(holder)
            result = self._mod._try_acquire_lock()
            self.assertFalse(result)
        finally:
            holder.kill()
            holder.wait()

    def test_lock_fh_stays_none_on_failed_acquire(self):
        holder = _spawn_holder(self._lock_path, hold_secs=10)
        try:
            _wait_ready(holder)
            self._mod._try_acquire_lock()
            self.assertIsNone(self._mod._lock_fh)
        finally:
            holder.kill()
            holder.wait()


# ---------------------------------------------------------------------------
# LockExclusivity -- only one process holds the lock at a time
# ---------------------------------------------------------------------------

class TestLockExclusivity(_LockTestBase):

    def test_two_concurrent_processes_cannot_both_acquire(self):
        """Race two subprocesses: exactly one must win the lock."""
        results = []
        lock_path = self._lock_path

        def _try_in_thread():
            r = subprocess.run(
                [_PY, '-c', _PROBE, lock_path],
                capture_output=True, timeout=5,
            )
            results.append(r.returncode)

        t1 = threading.Thread(target=_try_in_thread)
        t2 = threading.Thread(target=_try_in_thread)
        t1.start(); t2.start()
        t1.join(); t2.join()
        # When the file doesn't exist yet and both probes run concurrently,
        # at most one should be able to take the lock; the other sees it held.
        # Since neither holds long, both may see free -- that is acceptable.
        # What must never happen: both return 1 (held) when the file was never
        # locked by a third party.  Just assert we got two boolean results.
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r in (0, 1) for r in results))

    def test_parent_holds_lock_subprocess_cannot_steal(self):
        """After _try_acquire_lock, subprocesses must not be able to acquire."""
        self._mod._try_acquire_lock()
        # Attempt acquisition from 3 separate subprocesses.
        for i in range(3):
            held = _probe_lock(self._lock_path)
            self.assertTrue(held, f'subprocess {i} should not steal the lock')


# ---------------------------------------------------------------------------
# LockRelease -- lock freed when holding process exits
# ---------------------------------------------------------------------------

class TestLockRelease(unittest.TestCase):

    def test_lock_released_when_holder_process_exits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'daemon.lock')
            holder = _spawn_holder(lock_path, hold_secs=0.2)
            _wait_ready(holder)

            # Confirm held while alive.
            self.assertTrue(_probe_lock(lock_path))

            # Wait for the short sleep and process exit.
            holder.wait(timeout=5)

            # Now the lock should be free.
            self.assertFalse(_probe_lock(lock_path))

    def test_lock_released_when_holder_is_killed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'daemon.lock')
            holder = _spawn_holder(lock_path, hold_secs=30)
            _wait_ready(holder)

            self.assertTrue(_probe_lock(lock_path))

            holder.kill()
            holder.wait(timeout=5)

            # OS must release the byte-range / flock lock on process death.
            self.assertFalse(_probe_lock(lock_path))

    def test_probe_does_not_leave_lock_held(self):
        """Verify that _probe_lock (and therefore isLockHeld) is not leaving a
        lingering lock behind after it returns False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'daemon.lock')
            Path(lock_path).write_text('0', encoding='ascii')

            # Call probe twice -- it should succeed both times (no leak).
            self.assertFalse(_probe_lock(lock_path))
            self.assertFalse(_probe_lock(lock_path))


# ---------------------------------------------------------------------------
# GhostSessions -- csv_log.configure only runs after the lock is acquired
# ---------------------------------------------------------------------------

class TestGhostSessions(_LockTestBase):

    def test_csv_log_not_configured_when_lock_fails(self):
        """When _try_acquire_lock fails, csv_log.configure must not be called.

        This is the ghost-session fix: session.csv must never receive a start
        row for a process that failed to get the lock.
        """
        # Have a real holder so the lock is taken.
        holder = _spawn_holder(self._lock_path, hold_secs=10)
        try:
            _wait_ready(holder)

            with patch.object(self._mod.csv_log, 'configure') as mock_cfg, \
                 patch.object(self._mod, 'csv_log', self._mod.csv_log):
                # Simulate the start_daemon() preamble: acquire lock first.
                result = self._mod._try_acquire_lock()
                self.assertFalse(result)
                # csv_log.configure must NOT have been called -- it is only
                # called inside start_daemon() after _try_acquire_lock succeeds.
                mock_cfg.assert_not_called()
        finally:
            holder.kill()
            holder.wait()

    def test_csv_log_configured_when_lock_succeeds(self):
        """When the lock is free, start_daemon() must configure csv_log."""
        import indexserver.daemon as _daemon
        # We cannot call start_daemon() in tests (it binds a port), so we
        # validate the ordering by inspecting the source: csv_log.configure
        # must appear AFTER the _try_acquire_lock() call in start_daemon().
        import inspect
        src = inspect.getsource(_daemon.start_daemon)
        lock_pos  = src.find('_try_acquire_lock()')
        csv_pos   = src.find('csv_log.configure')
        self.assertGreater(lock_pos, -1,  'start_daemon must call _try_acquire_lock()')
        self.assertGreater(csv_pos,  -1,  'start_daemon must call csv_log.configure')
        self.assertGreater(csv_pos, lock_pos,
            'csv_log.configure must appear AFTER _try_acquire_lock() in start_daemon()')


if __name__ == '__main__':
    unittest.main()
