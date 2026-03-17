/**
 * Unit tests for the pure status-parsing helper in status.ts.
 *
 * Run with: npm test
 *
 * Covers:
 *   - watcher state pass-through (watching, paused, stopped, unknown)
 *   - "paused (windows fs watcher active)" label when VS Code watcher owns delivery
 *   - queue depth forwarding
 *   - verifier running detection (from verifyStatus.running AND status.verifier.state)
 *   - verifier progress counters
 *   - null / missing fields degrade gracefully
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

// buildStatusDetail is a pure function — no vscode APIs, safe to import directly.
import { buildStatusDetail } from '../status';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeWatcher(overrides: {
    state?: string;
    paused?: boolean;
    queue_depth?: number;
} = {}): { state?: string; paused?: boolean; queue_depth?: number } {
    return { state: 'watching', paused: false, queue_depth: 0, ...overrides };
}

function makeVerify(overrides: {
    running?: boolean;
    fs_files?: number;
    indexed?: number;
    missing?: number;
    stale?: number;
} = {}): typeof overrides {
    return { running: false, fs_files: 0, indexed: 0, missing: 0, stale: 0, ...overrides };
}

// ---------------------------------------------------------------------------
// Watcher state
// ---------------------------------------------------------------------------

describe('buildStatusDetail — watcherState', () => {
    it('passes through "watching" when VS Code watcher is inactive', () => {
        const d = buildStatusDetail(makeWatcher({ state: 'watching' }), undefined, null, false);
        assert.equal(d.watcherState, 'watching');
    });

    it('passes through "watching" even when VS Code watcher is active', () => {
        const d = buildStatusDetail(makeWatcher({ state: 'watching' }), undefined, null, true);
        assert.equal(d.watcherState, 'watching');
    });

    it('passes through "stopped" unchanged', () => {
        const d = buildStatusDetail(makeWatcher({ state: 'stopped' }), undefined, null, false);
        assert.equal(d.watcherState, 'stopped');
    });

    it('defaults to "unknown" when watcher field is missing', () => {
        const d = buildStatusDetail(undefined, undefined, null, false);
        assert.equal(d.watcherState, 'unknown');
    });

    it('defaults to "unknown" when watcher.state is undefined', () => {
        const d = buildStatusDetail({ paused: false, queue_depth: 0 }, undefined, null, false);
        assert.equal(d.watcherState, 'unknown');
    });

    it('shows "paused (windows fs watcher active)" when paused and VS Code watcher is active', () => {
        const d = buildStatusDetail(makeWatcher({ state: 'paused' }), undefined, null, true);
        assert.equal(d.watcherState, 'paused (windows fs watcher active)');
    });

    it('shows plain "paused" when paused but VS Code watcher is inactive', () => {
        const d = buildStatusDetail(makeWatcher({ state: 'paused' }), undefined, null, false);
        assert.equal(d.watcherState, 'paused');
    });
});

// ---------------------------------------------------------------------------
// Queue depth
// ---------------------------------------------------------------------------

describe('buildStatusDetail — queueDepth', () => {
    it('is 0 when no watcher field', () => {
        const d = buildStatusDetail(undefined, undefined, null, false);
        assert.equal(d.queueDepth, 0);
    });

    it('forwards non-zero queue depth', () => {
        const d = buildStatusDetail(makeWatcher({ queue_depth: 7 }), undefined, null, false);
        assert.equal(d.queueDepth, 7);
    });
});

// ---------------------------------------------------------------------------
// Verifier running
// ---------------------------------------------------------------------------

describe('buildStatusDetail — verifierRunning', () => {
    it('is false when both sources report not running', () => {
        const d = buildStatusDetail(makeWatcher(), { state: 'idle' }, makeVerify({ running: false }), false);
        assert.equal(d.verifierRunning, false);
    });

    it('is true when verifyStatus.running is true', () => {
        const d = buildStatusDetail(makeWatcher(), undefined, makeVerify({ running: true }), false);
        assert.equal(d.verifierRunning, true);
    });

    it('is true when status.verifier.state is "running" (verifyStatus absent)', () => {
        const d = buildStatusDetail(makeWatcher(), { state: 'running' }, null, false);
        assert.equal(d.verifierRunning, true);
    });

    it('is true when status.verifier.state is "running" even if verifyStatus says false', () => {
        const d = buildStatusDetail(makeWatcher(), { state: 'running' }, makeVerify({ running: false }), false);
        assert.equal(d.verifierRunning, true);
    });
});

// ---------------------------------------------------------------------------
// Verifier progress counters
// ---------------------------------------------------------------------------

describe('buildStatusDetail — verifier counters', () => {
    it('uses indexed for verifierChecked when present', () => {
        const d = buildStatusDetail(makeWatcher(), undefined, makeVerify({ indexed: 42, fs_files: 100 }), false);
        assert.equal(d.verifierChecked, 42);
        assert.equal(d.verifierTotal, 100);
    });

    it('falls back to fs_files for verifierChecked when indexed is absent', () => {
        const d = buildStatusDetail(makeWatcher(), undefined, makeVerify({ fs_files: 50 }), false);
        assert.equal(d.verifierChecked, 50);
    });

    it('forwards missing and stale counts', () => {
        const d = buildStatusDetail(makeWatcher(), undefined, makeVerify({ missing: 3, stale: 1 }), false);
        assert.equal(d.verifierMissing, 3);
        assert.equal(d.verifierStale, 1);
    });

    it('all counters are 0 when verifyStatus is null', () => {
        const d = buildStatusDetail(makeWatcher(), undefined, null, false);
        assert.equal(d.verifierChecked, 0);
        assert.equal(d.verifierTotal, 0);
        assert.equal(d.verifierMissing, 0);
        assert.equal(d.verifierStale, 0);
    });
});
