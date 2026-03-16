/**
 * Pipeline integration tests — validate the full Typesense + AST search
 * pipeline, mirroring exactly what the VS Code extension does.
 *
 * Start the server first:  ts start
 * Run all tests:           npm test
 * Run just this file:      node --require tsx/cjs --test src/test/pipeline.test.ts
 *
 * Live pipeline tests require the server AND these environment variables:
 *
 *   CS_QUERY      Symbol/type to search for (required for live tests)
 *   CS_SUB        Subsystem filter, e.g. "myapp" (optional, default: '')
 *
 * Example:
 *   CS_QUERY=BlobStore CS_SUB=absblobstore npm run test:pipeline
 *
 * If CS_QUERY is not set the live tests are skipped automatically.
 */

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as http from 'node:http';

import {
    CodesearchConfig,
    loadConfig,
    getRoots,
    runSearchPipeline,
    renderTextTree,
    computeMatchItems,
    MODES,
    TypesenseHit,
} from '../client';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function readConfig(): { config: CodesearchConfig; rootPath: string } | null {
    try {
        const p = path.resolve(__dirname, '../../../config.json');
        if (!fs.existsSync(p)) { return null; }
        const config = loadConfig(p);
        const rootPath = Object.values(getRoots(config))[0] ?? '';
        return { config, rootPath };
    } catch {
        return null;
    }
}

async function serverIsUp(port: number, apiKey: string): Promise<boolean> {
    return new Promise((resolve) => {
        const req = http.request(
            { hostname: 'localhost', port, path: '/health', method: 'GET',
              headers: { 'X-TYPESENSE-API-KEY': apiKey } },
            (res) => {
                let d = '';
                res.on('data', (c) => (d += c));
                res.on('end', () => { try { resolve(JSON.parse(d).ok === true); } catch { resolve(false); } });
            });
        req.setTimeout(2000, () => { req.destroy(); resolve(false); });
        req.on('error', () => resolve(false));
        req.end();
    });
}

async function queryApiIsUp(port: number): Promise<boolean> {
    return new Promise((resolve) => {
        const req = http.request(
            { hostname: 'localhost', port: port + 1, path: '/status', method: 'GET' },
            (res) => { res.resume(); resolve(res.statusCode !== undefined); });
        req.setTimeout(2000, () => { req.destroy(); resolve(false); });
        req.on('error', () => resolve(false));
        req.end();
    });
}

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

let cfg: CodesearchConfig;
let rootPath: string;
let serverAvailable = false;
let astAvailable = false;

// Env-var parameters for live pipeline tests — no defaults so tests skip if unset
const LIVE_QUERY = process.env['CS_QUERY'] ?? '';
const LIVE_SUB   = process.env['CS_SUB']   ?? '';

before(async () => {
    const loaded = readConfig();
    if (!loaded) { return; }
    cfg = loaded.config;
    rootPath = loaded.rootPath;
    const port = cfg.port ?? 8108;
    const apiKey = cfg.api_key ?? 'codesearch-local';
    serverAvailable = await serverIsUp(port, apiKey);
    if (serverAvailable) { astAvailable = await queryApiIsUp(port); }
});

function skipIfNoServer(t: { skip(msg?: string): void }): boolean {
    if (!serverAvailable) { t.skip('Typesense not running — run: ts start'); return true; }
    return false;
}

function skipIfNoLiveParams(t: { skip(msg?: string): void }): boolean {
    if (!serverAvailable) { t.skip('Typesense not running — run: ts start'); return true; }
    if (!LIVE_QUERY) { t.skip('CS_QUERY not set — set CS_QUERY=<symbol> to run live tests'); return true; }
    return false;
}

function skipIfNoAst(t: { skip(msg?: string): void }): boolean {
    if (skipIfNoLiveParams(t)) { return true; }
    if (!astAvailable) { t.skip('AST API not running (port+1)'); return true; }
    return false;
}

function printTree(tree: string): void {
    process.stderr.write('\n' + tree + '\n');
}

// ---------------------------------------------------------------------------
// computeMatchItems — pure unit tests (no server, no codebase-specific names)
// ---------------------------------------------------------------------------

describe('computeMatchItems — sig mode', () => {
    // Sig mode uses highlights.values (the sigs Typesense matched), not all sigs.
    function makeHit(matchedSigs: string[]): TypesenseHit {
        return {
            document: { id: '1', relative_path: 'Service.cs', filename: 'Service.cs',
                method_sigs: ['void Unrelated()', ...matchedSigs] },
            highlights: matchedSigs.length > 0
                ? [{ field: 'method_sigs', values: matchedSigs }]
                : [],
        };
    }

    it('returns only matched method_sigs via highlights.values', () => {
        const matched = [
            'void GetItems(string key, List<Item> items)',
            'Task WriteAsync(string key, Stream data)',
        ];
        const items = computeMatchItems(makeHit(matched), 'sig');
        assert.equal(items.length, 2);
        assert.ok(items[0].text.includes('GetItems'));
        assert.ok(items[1].text.includes('WriteAsync'));
    });

    it('every sig item has no line number (comes from Typesense index, not AST)', () => {
        const items = computeMatchItems(makeHit(['void Foo()', 'Task BarAsync()']), 'sig');
        for (const item of items) {
            assert.equal(item.line, undefined, 'sig items must not have line numbers');
        }
    });

    it('returns empty array when no sigs matched', () => {
        assert.deepEqual(computeMatchItems(makeHit([]), 'sig'), []);
    });
});

describe('computeMatchItems — array modes use highlights.values', () => {
    // All array-field modes prefer highlights.values (matched elements) and fall
    // back to the raw doc array only for implements/uses/attrs/casts (not sig/find/params).
    function makeHit(hlField: string, hlValues: string[]): TypesenseHit {
        return {
            document: {
                id: '1', relative_path: 'Service.cs', filename: 'Service.cs',
                class_names:  ['MyService', 'MyServiceConfig'],
                method_names: ['GetAsync', 'SetAsync'],
                base_types:   ['IMyService', 'IDisposable', 'UnrelatedBase'],
                type_refs:    ['IMyService', 'MyConfig', 'UnrelatedRef'],
                attributes:   ['Authorize', 'UnrelatedAttr'],
                cast_sites:   ['(IMyService)instance', '(UnrelatedType)x'],
                method_sigs:  ['void GetAsync()', 'Task SetAsync(int id)', 'void Unrelated()'],
            },
            highlights: [{ field: hlField, values: hlValues }],
        };
    }

    it('implements mode uses highlights.values for base_types', () => {
        const items = computeMatchItems(makeHit('base_types', ['IMyService']), 'implements');
        assert.deepEqual(items.map((i) => i.text), ['IMyService']);
    });

    it('attrs mode uses highlights.values for attributes', () => {
        const items = computeMatchItems(makeHit('attributes', ['Authorize']), 'attrs');
        assert.deepEqual(items.map((i) => i.text), ['Authorize']);
    });

    it('casts mode uses highlights.values for cast_sites', () => {
        const items = computeMatchItems(makeHit('cast_sites', ['(IMyService)instance']), 'casts');
        assert.deepEqual(items.map((i) => i.text), ['(IMyService)instance']);
    });

    it('symbols mode returns class_names then method_names (no highlights needed)', () => {
        const items = computeMatchItems(makeHit('class_names', ['MyService']), 'symbols');
        assert.ok(items.some((i) => i.text === 'MyService'));
        assert.ok(items.some((i) => i.text === 'GetAsync'));
    });

    it('find mode uses highlights.values for method_sigs', () => {
        const items = computeMatchItems(makeHit('method_sigs', ['void GetAsync()']), 'find');
        assert.deepEqual(items.map((i) => i.text), ['void GetAsync()']);
    });

    it('uses/field_type/param_type/ident/member_accesses use highlights.values for type_refs', () => {
        for (const mode of ['uses', 'field_type', 'param_type', 'ident', 'member_accesses']) {
            const items = computeMatchItems(makeHit('type_refs', ['IMyService']), mode);
            assert.deepEqual(items.map((i) => i.text), ['IMyService'],
                `${mode}: expected only highlighted type_ref`);
        }
    });
});

// ---------------------------------------------------------------------------
// renderTextTree — pure unit tests
// ---------------------------------------------------------------------------

describe('renderTextTree', () => {
    it('renders subsystem header, filename, and match items with line numbers', () => {
        const result = {
            hits: [{
                document: { id: '1', relative_path: 'myapp/Service.cs',
                            filename: 'Service.cs', subsystem: 'myapp' },
                _matches: [
                    { text: 'void GetItems(...)', line: 9 },
                    { text: 'Task WriteAsync(...)', line: 24 },
                ],
            }],
            found: 1, tsFound: 3, elapsed: 12, facet_counts: [],
        };
        const tree = renderTextTree(result, 'IService', 'sig');
        assert.ok(tree.includes('[myapp]'),          'expected subsystem header');
        assert.ok(tree.includes('Service.cs'),       'expected filename');
        assert.ok(tree.includes('GetItems'),         'expected match text');
        assert.ok(tree.includes(':10'),              'expected 1-indexed line number');
    });

    it('shows "(no results)" when found is 0', () => {
        const result = { hits: [], found: 0, tsFound: 0, elapsed: 5, facet_counts: [] };
        assert.ok(renderTextTree(result, 'Foo', 'sig').includes('(no results)'));
    });

    it('annotates AST modes with "Typesense: N"', () => {
        const result = { hits: [], found: 0, tsFound: 7, elapsed: 5, facet_counts: [] };
        assert.ok(renderTextTree(result, 'Foo', 'uses').includes('Typesense: 7'));
    });

    it('does not show Typesense annotation for search-only modes', () => {
        const result = { hits: [], found: 0, tsFound: 5, elapsed: 5, facet_counts: [] };
        assert.ok(!renderTextTree(result, 'Foo', 'sig').includes('Typesense:'));
    });

    it('truncates long match lists with "… N more matches"', () => {
        const matches = Array.from({ length: 15 }, (_, i) => ({ text: `match${i}`, line: i }));
        const result = {
            hits: [{ document: { id: '1', relative_path: 'a/B.cs',
                                  filename: 'B.cs', subsystem: 'a' }, _matches: matches }],
            found: 1, tsFound: 1, elapsed: 1, facet_counts: [],
        };
        const tree = renderTextTree(result, 'x', 'ident');
        assert.ok(tree.includes('… 5 more matches'), `expected truncation note, got:\n${tree}`);
    });
});

// ---------------------------------------------------------------------------
// MODES — sig is search-only, AST modes have astMode
// ---------------------------------------------------------------------------

describe('MODES structure', () => {
    it('sig mode has no astMode (search-only)', () => {
        assert.equal(MODES.find((m) => m.key === 'sig')!.astMode, undefined);
    });

    it('uses mode has astMode=uses', () => {
        assert.equal(MODES.find((m) => m.key === 'uses')!.astMode, 'uses');
    });

    it('every AST-backed mode has astMode equal to its key', () => {
        for (const m of MODES.filter((m) => m.astMode !== undefined)) {
            assert.equal(m.astMode, m.key, `${m.key}: astMode !== key`);
        }
    });
});

// ---------------------------------------------------------------------------
// Live pipeline tests — require CS_QUERY env var + running server
//
// These tests are generic: they exercise the pipeline contract regardless of
// what codebase is indexed. The query and subsystem come from env vars so
// this file has no knowledge of any specific project.
// ---------------------------------------------------------------------------

describe('pipeline — sig mode (search-only, CS_QUERY)', () => {
    it('returns method_sigs from the index; each item has parens, no line number', async (t) => {
        if (skipIfNoLiveParams(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'sig', 'cs', LIVE_SUB, '', 20, rootPath);
        printTree(renderTextTree(result, `${LIVE_QUERY} [sub=${LIVE_SUB || '*'}]`, 'sig'));

        assert.ok(result.found > 0, `no sig results for "${LIVE_QUERY}"`);
        assert.ok(result.found >= result.hits.length);

        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0, `${hit.document.relative_path}: no match items`);
            for (const m of hit._matches) {
                assert.ok(m.text.includes('('),
                    `sig item has no parens — not a signature: "${m.text}"`);
                assert.equal(m.line, undefined,
                    `sig item has unexpected line number ${m.line} — sig must be search-only`);
            }
        }
    });
});

describe('pipeline — uses mode (AST-backed, CS_QUERY)', () => {
    it('returns exact line-level matches; all files have at least one match', async (t) => {
        if (skipIfNoAst(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 20, rootPath);
        printTree(renderTextTree(result, `${LIVE_QUERY} [sub=${LIVE_SUB || '*'}]`, 'uses'));

        assert.ok(result.found > 0, `no uses results for "${LIVE_QUERY}"`);
        assert.ok(result.tsFound >= result.found,
            `tsFound (${result.tsFound}) < found (${result.found})`);

        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0,
                `${hit.document.relative_path}: AST filter left no matches (should be filtered out)`);
            for (const m of hit._matches) {
                assert.ok(typeof m.line === 'number' && m.line >= 0,
                    `${hit.document.relative_path}: invalid line ${m.line}`);
            }
        }
    });

    it('no hit has zero matches (empty-match filter works)', async (t) => {
        if (skipIfNoAst(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 20, rootPath);
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0,
                `${hit.document.relative_path}: in results with 0 matches`);
        }
    });
});

describe('pipeline — implements mode (AST-backed, CS_QUERY)', () => {
    it('runs without error; all returned files have at least one match', async (t) => {
        if (skipIfNoAst(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'implements', 'cs', LIVE_SUB, '', 20, rootPath);
        printTree(renderTextTree(result, `${LIVE_QUERY} [implements]`, 'implements'));
        // May legitimately return 0 if nothing inherits the type
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0,
                `${hit.document.relative_path}: empty matches in implements result`);
        }
    });
});

describe('pipeline — expansion simulation (CS_QUERY + CS_SUB)', () => {
    it('sig expand: all returned hits belong to the requested subsystem', async (t) => {
        if (skipIfNoLiveParams(t)) { return; }
        if (!LIVE_SUB) { t.skip('CS_SUB not set — set CS_SUB=<subsystem> to test expansion'); return; }

        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'sig', 'cs', LIVE_SUB, '', 50, rootPath);
        printTree(renderTextTree(result, `${LIVE_QUERY} [expand: ${LIVE_SUB}]`, 'sig'));

        assert.ok(result.found > 0, `no results for ${LIVE_QUERY} in ${LIVE_SUB}`);
        for (const hit of result.hits) {
            assert.equal(hit.document.subsystem, LIVE_SUB,
                `unexpected subsystem ${hit.document.subsystem} in ${hit.document.relative_path}`);
        }
    });

    it('uses expand: AST-filtered, line numbers present', async (t) => {
        if (skipIfNoAst(t)) { return; }
        if (!LIVE_SUB) { t.skip('CS_SUB not set'); return; }

        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 50, rootPath);
        printTree(renderTextTree(result, `${LIVE_QUERY} [expand: ${LIVE_SUB}]`, 'uses'));

        assert.ok(result.found > 0);
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0);
            assert.ok(hit._matches.some((m) => typeof m.line === 'number'));
        }
    });
});
