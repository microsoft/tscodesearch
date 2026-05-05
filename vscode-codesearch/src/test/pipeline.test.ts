/**
 * Pipeline integration tests — validate the full Typesense + AST search
 * pipeline, mirroring exactly what the VS Code extension does.
 *
 * Start the server first:  ts start
 * Run all tests:           npm test
 * Run just this file:      node --require tsx/cjs --test src/test/pipeline.test.ts
 *
 * Live pipeline tests require the server AND the management API (port+1).
 * They always query against the sample data in sample/root1/ and sample/root2/
 * using hardcoded type names from those fixtures. They skip automatically when
 * the server is not reachable.
 *
 * Optional env vars:
 *   CS_CONFIG   Path to config.json (defaults to ../../../config.json)
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
    doQuerySingleFile,
    resolveFilePath,
    renderTextTree,
    MODES,
} from '../client';

// ---------------------------------------------------------------------------
// Fixed sample-data queries
// These symbols exist in sample/root1/ and sample/root2/.
// ---------------------------------------------------------------------------

const DECLARATIONS_QUERY = 'IProcessor'; // defined in Processors.cs and root2/Widgets.cs
const USES_QUERY         = 'IDataStore'; // used extensively in DataStore.cs
const IMPLEMENTS_QUERY   = 'IDataStore'; // implemented by SqlDataStore in DataStore.cs

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function readConfig(): { config: CodesearchConfig; rootPath: string } | null {
    try {
        const envPath = process.env['CS_CONFIG'];
        const p = envPath
            ? path.resolve(envPath)
            : path.resolve(__dirname, '../../../config.json');
        if (!fs.existsSync(p)) { return null; }
        const config = loadConfig(p);
        const rootPath = Object.values(getRoots(config))[0] ?? '';
        return { config, rootPath };
    } catch {
        return null;
    }
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
let rootName: string;
let serverAvailable = false;

before(async () => {
    const loaded = readConfig();
    if (!loaded) { return; }
    cfg = loaded.config;

    // Live pipeline tests require a root named "sample" in the config.
    // All test runner modes (WSL, Linux, Docker) add this root pointing to
    // sample/root1 so the collection codesearch_sample is always available.
    // If it is absent the config is not a test config (e.g. production
    // config.json was loaded as a fallback) — skip rather than assert against
    // production files.
    const sampleEntry = cfg.roots['sample'] as
        { path?: string } | undefined;
    if (!sampleEntry) { return; }

    rootName = 'sample';
    rootPath = (sampleEntry.path ?? '').replace(/\\/g, '/');
    const port = cfg.port ?? 8108;
    serverAvailable = await queryApiIsUp(port);
});

function skipIfNoServer(t: { skip(msg?: string): void }): boolean {
    if (!serverAvailable) { t.skip('Indexserver not running — run: ts start'); return true; }
    return false;
}

function printTree(tree: string): void {
    process.stderr.write('\n' + tree + '\n');
}

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
            found: 1, elapsed: 12, facet_counts: [],
        };
        const tree = renderTextTree(result, 'IService', 'declarations');
        assert.ok(tree.includes('[myapp]'),          'expected subsystem header');
        assert.ok(tree.includes('Service.cs'),       'expected filename');
        assert.ok(tree.includes('GetItems'),         'expected match text');
        assert.ok(tree.includes(':10'),              'expected 1-indexed line number');
    });

    it('shows "(no results)" when found is 0', () => {
        const result = { hits: [], found: 0, elapsed: 5, facet_counts: [] };
        assert.ok(renderTextTree(result, 'Foo', 'text').includes('(no results)'));
    });

    it('includes mode label in output', () => {
        const result = { hits: [], found: 0, elapsed: 5, facet_counts: [] };
        assert.ok(renderTextTree(result, 'Foo', 'uses').includes('Uses'));
    });

    it('truncates long match lists with "… N more matches"', () => {
        const matches = Array.from({ length: 15 }, (_, i) => ({ text: `match${i}`, line: i }));
        const result = {
            hits: [{ document: { id: '1', relative_path: 'a/B.cs',
                                  filename: 'B.cs', subsystem: 'a' }, _matches: matches }],
            found: 1, elapsed: 1, facet_counts: [],
        };
        const tree = renderTextTree(result, 'x', 'all_refs');
        assert.ok(tree.includes('… 5 more matches'), `expected truncation note, got:\n${tree}`);
    });
});

// ---------------------------------------------------------------------------
// MODES — text is search-only, AST modes have astMode
// ---------------------------------------------------------------------------

describe('MODES structure', () => {
    it('text mode has no astMode (search-only)', () => {
        assert.equal(MODES.find((m) => m.key === 'text')!.astMode, undefined);
    });

    it('uses mode has astMode=uses', () => {
        assert.equal(MODES.find((m) => m.key === 'uses')!.astMode, 'uses');
    });

    it('AST-backed modes (except uses_kind sub-modes) have astMode equal to key', () => {
        const skipKeys = new Set(['uses_field', 'uses_param']);
        for (const m of MODES.filter((m) => m.astMode !== undefined && !skipKeys.has(m.key))) {
            assert.equal(m.astMode, m.key, `${m.key}: astMode !== key`);
        }
    });

    it('uses_field and uses_param delegate to uses with uses_kind', () => {
        assert.equal(MODES.find((m) => m.key === 'uses_field')!.astMode, 'uses');
        assert.equal(MODES.find((m) => m.key === 'uses_param')!.astMode, 'uses');
    });
});

// ---------------------------------------------------------------------------
// Live pipeline tests — require a running server with sample data indexed
// ---------------------------------------------------------------------------

describe('pipeline — declarations mode (IProcessor)', () => {
    it('returns match items; each item has non-empty text', async (t) => {
        if (skipIfNoServer(t)) { return; }
        const result = await runSearchPipeline(
            cfg, DECLARATIONS_QUERY, 'declarations', 'cs', '', rootName, 20);
        printTree(renderTextTree(result, DECLARATIONS_QUERY, 'declarations'));

        assert.ok(result.found > 0, `no declarations results for "${DECLARATIONS_QUERY}"`);
        assert.ok(result.found >= result.hits.length);

        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0, `${hit.document.relative_path}: no match items`);
            for (const m of hit._matches) {
                assert.ok(m.text.length > 0, `declarations item is empty`);
            }
        }
    });
});

describe('pipeline — uses mode (IDataStore)', () => {
    it('returns exact line-level matches; all files have at least one match', async (t) => {
        if (skipIfNoServer(t)) { return; }
        const result = await runSearchPipeline(
            cfg, USES_QUERY, 'uses', 'cs', '', rootName, 20);
        printTree(renderTextTree(result, USES_QUERY, 'uses'));

        assert.ok(result.found > 0, `no uses results for "${USES_QUERY}"`);

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
        if (skipIfNoServer(t)) { return; }
        const result = await runSearchPipeline(
            cfg, USES_QUERY, 'uses', 'cs', '', rootName, 20);
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0,
                `${hit.document.relative_path}: in results with 0 matches`);
        }
    });
});

describe('pipeline — implements mode (IDataStore)', () => {
    it('runs without error; all returned files have at least one match', async (t) => {
        if (skipIfNoServer(t)) { return; }
        const result = await runSearchPipeline(
            cfg, IMPLEMENTS_QUERY, 'implements', 'cs', '', rootName, 20);
        printTree(renderTextTree(result, IMPLEMENTS_QUERY, 'implements'));
        assert.ok(result.found > 0, `no implements results for "${IMPLEMENTS_QUERY}"`);
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0,
                `${hit.document.relative_path}: empty matches in implements result`);
        }
    });
});

// ---------------------------------------------------------------------------
// doQuerySingleFile E2E — expansion round-trip against the live /query API
//
// These tests simulate exactly what the webview does when a file row with
// ast_expanded=false comes into view: call doQuerySingleFile on the file,
// then verify the returned matches are usable.
// ---------------------------------------------------------------------------

describe('expansion — doQuerySingleFile round-trip (IDataStore uses)', () => {
    it('expanding a file from search results yields valid line-number matches', async (t) => {
        if (skipIfNoServer(t)) { return; }

        // Step 1: search to get a result set, same as the webview does.
        const searchResult = await runSearchPipeline(
            cfg, USES_QUERY, 'uses', 'cs', '', rootName, 10);
        if (searchResult.hits.length === 0) {
            t.skip(`no "uses" results for "${USES_QUERY}" — index may not contain sample data`);
            return;
        }

        // Step 2: pick the first result and build the absolute path, same as the
        //         extension's expandFile handler does via resolveFilePath.
        const hit      = searchResult.hits[0];
        const relPath  = hit.document.relative_path;
        const absPath  = resolveFilePath(rootPath, relPath);

        // Step 3: call doQuerySingleFile — this is the call the extension makes.
        const matches = await doQuerySingleFile(cfg, 'uses', USES_QUERY, absPath);

        assert.ok(matches.length > 0,
            `doQuerySingleFile returned no matches for ${relPath} (query: "${USES_QUERY}")`);

        for (const m of matches) {
            assert.ok(typeof m.line === 'number' && m.line >= 0,
                `invalid line ${m.line} in match from ${relPath}`);
            assert.ok(typeof m.text === 'string' && m.text.length > 0,
                `empty text in match from ${relPath}`);
        }

        process.stderr.write(
            `\n[expansion E2E] ${relPath}: ${matches.length} match(es) via doQuerySingleFile\n`
            + matches.slice(0, 3).map((m) => `  :${m.line! + 1}  ${m.text.trim().slice(0, 80)}`).join('\n') + '\n',
        );
    });

    it('expanding with declarations mode returns signature-like matches', async (t) => {
        if (skipIfNoServer(t)) { return; }

        const searchResult = await runSearchPipeline(
            cfg, DECLARATIONS_QUERY, 'declarations', 'cs', '', rootName, 10);
        if (searchResult.hits.length === 0) {
            t.skip(`no "declarations" results for "${DECLARATIONS_QUERY}"`);
            return;
        }

        const hit     = searchResult.hits[0];
        const absPath = resolveFilePath(rootPath, hit.document.relative_path);
        const matches = await doQuerySingleFile(cfg, 'declarations', DECLARATIONS_QUERY, absPath);

        assert.ok(matches.length > 0,
            `doQuerySingleFile(declarations) returned no matches for ${hit.document.relative_path}`);
        for (const m of matches) {
            assert.ok(m.line! >= 0, `negative line number: ${m.line}`);
        }
    });

    it('expansion result line numbers are consistent with search pipeline line numbers', async (t) => {
        if (skipIfNoServer(t)) { return; }

        // Both search pipeline and single-file expansion should agree on line numbers
        // for fully-expanded hits (ast_expanded=true).
        const searchResult = await runSearchPipeline(
            cfg, USES_QUERY, 'uses', 'cs', '', rootName, 5);
        const hit = searchResult.hits.find((h) => h._matches.length > 0 && h.ast_expanded !== false);
        if (!hit) {
            t.skip('no fully-expanded hits in search results');
            return;
        }

        const absPath       = resolveFilePath(rootPath, hit.document.relative_path);
        const expandMatches = await doQuerySingleFile(cfg, 'uses', USES_QUERY, absPath);

        // The expansion must include every line the pipeline already found for this file.
        const expandLines = new Set(expandMatches.map((m) => m.line));
        for (const m of hit._matches) {
            assert.ok(expandLines.has(m.line),
                `pipeline returned line ${m.line} for ${hit.document.relative_path} `
                + `but doQuerySingleFile missed it`);
        }
    });

    it('expansion on a non-existent file returns empty matches (not an error)', async (t) => {
        if (skipIfNoServer(t)) { return; }

        // A file path that doesn't exist — server should return empty results gracefully.
        const fakeAbsPath = resolveFilePath(rootPath, '__nonexistent__/DoesNotExist.cs');
        const matches = await doQuerySingleFile(cfg, 'calls', USES_QUERY, fakeAbsPath);
        assert.deepEqual(matches, [], 'expected empty array for non-existent file');
    });
});
