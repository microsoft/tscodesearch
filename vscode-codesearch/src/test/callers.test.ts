/**
 * Unit tests for enrichCallersHits (caller-site enrichment).
 *
 * Tests the text-based regex scan used to find call sites with line numbers.
 * No VS Code or Typesense required — runs entirely against local files.
 *
 * Run with:  npm test
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import * as path from 'node:path';
import * as os from 'node:os';
import * as fs from 'node:fs';

import { enrichCallersHits, CallerSite } from '../client';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FIXTURE_DIR  = __dirname;
const FIXTURE_FILE = 'callers_fixture.cs';

/** Build a minimal fake TypesenseHit for a relative path under rootPath. */
function makeHit(relativePath: string) {
    return {
        document: {
            id: '1',
            relative_path: relativePath,
            filename: path.basename(relativePath),
        },
        highlights: [],
    };
}

/** Run enrichment on a single fixture file, return its callerSites. */
function findSites(query: string, fixturePath = FIXTURE_FILE): CallerSite[] {
    const hit = makeHit(fixturePath);
    const result = enrichCallersHits([hit], query, FIXTURE_DIR);
    return (result[0] as any)._callerSites as CallerSite[];
}

// ---------------------------------------------------------------------------
// Basic matching
// ---------------------------------------------------------------------------

describe('enrichCallersHits — basic matching', () => {
    it('finds all GetBlobsAsync call sites', () => {
        const sites = findSites('GetBlobsAsync');
        const lines = sites.map((s) => s.line);

        // interface declaration — text-only regex cannot distinguish from a call site
        assert.ok(lines.includes(9),  'interface declaration (line 9)');
        // direct awaited call
        assert.ok(lines.includes(21), 'direct await call (line 21)');
        // ConfigureAwait pattern
        assert.ok(lines.includes(27), 'ConfigureAwait (line 27)');
        // .Result synchronous access
        assert.ok(lines.includes(33), '.Result call (line 33)');
        // string literal — known text-only limitation
        assert.ok(lines.includes(49), 'string literal false-positive (line 49)');
        // Moq Setup (first line of multi-line call)
        assert.ok(lines.includes(62), 'mock Setup (line 62)');
        // Moq Verify
        assert.ok(lines.includes(70), 'mock Verify (line 70)');
        // lowercase (case-insensitive)
        assert.ok(lines.includes(77), 'lowercase call (line 77)');
    });

    it('returns trimmed line text', () => {
        const sites = findSites('GetBlobsAsync');
        const line21 = sites.find((s) => s.line === 21);
        assert.ok(line21, 'site at line 21');
        assert.equal(line21!.text, 'return await _store.GetBlobsAsync(container, null, null);');
    });

    it('lines are 0-indexed', () => {
        // The fixture has "return await _store.GetBlobsAsync..." on file line 22 (1-indexed)
        // which is index 21 (0-indexed)
        const sites = findSites('GetBlobsAsync');
        assert.ok(sites.some((s) => s.line === 21));
    });
});

// ---------------------------------------------------------------------------
// Word-boundary and non-matching
// ---------------------------------------------------------------------------

describe('enrichCallersHits — non-matching', () => {
    it('does not match GetAwaiter when searching for GetBlobsAsync', () => {
        const sites = findSites('GetBlobsAsync');
        assert.ok(!sites.some((s) => s.text.includes('GetAwaiter')),
            'GetAwaiter should not appear in GetBlobsAsync results');
    });

    it('does not match GetResult when searching for GetBlobsAsync', () => {
        const sites = findSites('GetBlobsAsync');
        assert.ok(!sites.some((s) => s.text.includes('GetResult')),
            'GetResult should not appear in GetBlobsAsync results');
    });

    it('does not match GetBlobAsync (singular) when searching for GetBlobsAsync', () => {
        // GetBlobAsync does NOT end with "s" — a different method
        const sites = findSites('GetBlobsAsync');
        const texts = sites.map((s) => s.text);
        // None of the matched lines should be ONLY GetBlobAsync (not GetBlobsAsync)
        assert.ok(!texts.some((t) => /\bGetBlobAsync\s*\(/.test(t) && !/GetBlobsAsync/.test(t)),
            'pure GetBlobAsync calls should not appear in GetBlobsAsync results');
    });

    it('word boundary: GetBlobsAsyncHelper does not match GetBlobsAsync', () => {
        const sites = findSites('GetBlobsAsync');
        assert.ok(!sites.some((s) => s.text.includes('GetBlobsAsyncHelper')),
            'GetBlobsAsyncHelper should not match due to word boundary');
    });

    it('GetAwaiter search finds its own call site', () => {
        const sites = findSites('GetAwaiter');
        assert.equal(sites.length, 1);
        assert.equal(sites[0].line, 45);
        assert.ok(sites[0].text.includes('GetAwaiter'));
    });

    it('GetResult search finds its own call site', () => {
        const sites = findSites('GetResult');
        assert.equal(sites.length, 1);
        assert.equal(sites[0].line, 46);
    });
});

// ---------------------------------------------------------------------------
// Case insensitivity
// ---------------------------------------------------------------------------

describe('enrichCallersHits — case insensitive', () => {
    it('lowercase query matches mixed-case call sites', () => {
        const upper = findSites('GetBlobsAsync').map((s) => s.line);
        const lower = findSites('getblobsasync').map((s) => s.line);
        assert.deepEqual(upper.sort((a,b) => a-b), lower.sort((a,b) => a-b));
    });

    it('uppercase query matches lowercase call in fixture', () => {
        const sites = findSites('GetBlobsAsync');
        // line 77: _mockStore.Setup(x => x.getblobsasync(...))
        assert.ok(sites.some((s) => s.line === 77));
    });
});

// ---------------------------------------------------------------------------
// Qualified method name
// ---------------------------------------------------------------------------

describe('enrichCallersHits — qualified query (Receiver.Method)', () => {
    it('Store.GetBlobsAsync extracts GetBlobsAsync and finds same sites', () => {
        const plain     = findSites('GetBlobsAsync').map((s) => s.line).sort((a,b) => a-b);
        const qualified = findSites('_store.GetBlobsAsync').map((s) => s.line).sort((a,b) => a-b);
        assert.deepEqual(plain, qualified);
    });

    it('deep qualifier: a.b.c.Method extracts Method', () => {
        const sites = findSites('a.b.c.GetBlobsAsync');
        assert.ok(sites.length > 0, 'should find sites via deep qualifier');
    });
});

// ---------------------------------------------------------------------------
// Generic method calls  (Method<T>)
// ---------------------------------------------------------------------------

describe('enrichCallersHits — generic calls', () => {
    it('matches generic call GetItemsAsync<string>(', () => {
        const sites = findSites('GetItemsAsync');
        // line 39: var items = await _store.GetItemsAsync<string>("container");
        assert.ok(sites.some((s) => s.line === 39), `expected line 39, got ${JSON.stringify(sites)}`);
    });

    it('does not match GetItemsAsync when searching for GetBlobsAsync', () => {
        const sites = findSites('GetBlobsAsync');
        assert.ok(!sites.some((s) => s.text.includes('GetItemsAsync')));
    });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe('enrichCallersHits — edge cases', () => {
    it('empty query returns empty _callerSites', () => {
        const sites = findSites('');
        assert.equal(sites.length, 0);
    });

    it('whitespace-only query returns empty _callerSites', () => {
        const sites = findSites('   ');
        assert.equal(sites.length, 0);
    });

    it('non-existent file returns empty _callerSites without throwing', () => {
        const hit = makeHit('does_not_exist.cs');
        const result = enrichCallersHits([hit], 'GetBlobsAsync', FIXTURE_DIR);
        assert.equal((result[0] as any)._callerSites.length, 0);
    });

    it('non-existent root dir returns empty _callerSites without throwing', () => {
        const hit = makeHit(FIXTURE_FILE);
        const result = enrichCallersHits([hit], 'GetBlobsAsync', '/no/such/dir');
        assert.equal((result[0] as any)._callerSites.length, 0);
    });

    it('processes multiple hits independently', () => {
        const hits = [makeHit(FIXTURE_FILE), makeHit(FIXTURE_FILE)];
        const results = enrichCallersHits(hits, 'GetBlobsAsync', FIXTURE_DIR);
        assert.equal(results.length, 2);
        const a = (results[0] as any)._callerSites as CallerSite[];
        const b = (results[1] as any)._callerSites as CallerSite[];
        assert.deepEqual(a.map(s => s.line), b.map(s => s.line));
    });

    it('each hit gets its own _callerSites array (not shared reference)', () => {
        const hits = [makeHit(FIXTURE_FILE), makeHit(FIXTURE_FILE)];
        const results = enrichCallersHits(hits, 'GetBlobsAsync', FIXTURE_DIR);
        const a = (results[0] as any)._callerSites as CallerSite[];
        const b = (results[1] as any)._callerSites as CallerSite[];
        assert.notEqual(a, b);
    });
});

// ---------------------------------------------------------------------------
// Path resolution — WSL root path
// ---------------------------------------------------------------------------

describe('enrichCallersHits — path resolution', () => {
    it('resolves WSL-style root path (/mnt/x/...) to Windows path', () => {
        // Convert FIXTURE_DIR (e.g. Q:/...) to a fake WSL path /mnt/q/...
        const drive = FIXTURE_DIR[0].toLowerCase();
        const rest  = FIXTURE_DIR.slice(2).replace(/\\/g, '/');
        const wslRoot = `/mnt/${drive}${rest}`;
        const hit = makeHit(FIXTURE_FILE);
        const results = enrichCallersHits([hit], 'GetBlobsAsync', wslRoot);
        const sites = (results[0] as any)._callerSites as CallerSite[];
        assert.ok(sites.length > 0, 'WSL path should resolve and find sites');
    });
});

// ---------------------------------------------------------------------------
// Inline fixture via temp file
// ---------------------------------------------------------------------------

describe('enrichCallersHits — inline temp-file fixtures', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-caller-test-'));

    it('single call on a known line', () => {
        const src = [
            'public class Foo {',
            '    public void Bar() {',
            '        svc.DoThing();',    // line 2 (0-indexed)
            '    }',
            '}',
        ].join('\n');
        const f = path.join(tmpDir, 'single.cs');
        fs.writeFileSync(f, src);
        const sites = findSites('DoThing', 'single.cs');
        // findSites uses FIXTURE_DIR, so write to FIXTURE_DIR for this test
        // Actually write to FIXTURE_DIR so findSites works
        const dest = path.join(FIXTURE_DIR, 'tmp_single.cs');
        fs.writeFileSync(dest, src);
        try {
            const s = findSites('DoThing', 'tmp_single.cs');
            assert.equal(s.length, 1);
            assert.equal(s[0].line, 2);
            assert.equal(s[0].text, 'svc.DoThing();');
        } finally {
            fs.unlinkSync(dest);
        }
    });

    it('no matches returns empty array', () => {
        const src = 'public class Empty { }';
        const dest = path.join(FIXTURE_DIR, 'tmp_empty.cs');
        fs.writeFileSync(dest, src);
        try {
            const s = findSites('NonExistentMethod', 'tmp_empty.cs');
            assert.equal(s.length, 0);
        } finally {
            fs.unlinkSync(dest);
        }
    });

    it('three consecutive calls on successive lines', () => {
        const src = [
            'class T {',
            '    void A() { svc.Foo(); }',   // line 1
            '    void B() { svc.Foo(); }',   // line 2
            '    void C() { svc.Foo(); }',   // line 3
            '}',
        ].join('\n');
        const dest = path.join(FIXTURE_DIR, 'tmp_multi.cs');
        fs.writeFileSync(dest, src);
        try {
            const s = findSites('Foo', 'tmp_multi.cs');
            assert.equal(s.length, 3);
            assert.deepEqual(s.map(x => x.line), [1, 2, 3]);
        } finally {
            fs.unlinkSync(dest);
        }
    });

    it('method called with generic type arg Method<T>(', () => {
        const src = [
            'class T {',
            '    void A() { svc.Parse<int>(str); }',  // line 1
            '}',
        ].join('\n');
        const dest = path.join(FIXTURE_DIR, 'tmp_generic.cs');
        fs.writeFileSync(dest, src);
        try {
            const s = findSites('Parse', 'tmp_generic.cs');
            assert.equal(s.length, 1);
            assert.equal(s[0].line, 1);
        } finally {
            fs.unlinkSync(dest);
        }
    });

    it('method name that is a suffix of another does not match (word boundary)', () => {
        const src = [
            'class T {',
            '    void A() { PrefixFoo();  }',  // should NOT match Foo
            '    void B() { svc.Foo();    }',  // should match Foo
            '    void C() { FooSuffix();  }',  // should NOT match Foo
            '}',
        ].join('\n');
        const dest = path.join(FIXTURE_DIR, 'tmp_boundary.cs');
        fs.writeFileSync(dest, src);
        try {
            const s = findSites('Foo', 'tmp_boundary.cs');
            assert.equal(s.length, 1);
            assert.equal(s[0].line, 2);
        } finally {
            fs.unlinkSync(dest);
        }
    });
});

