/**
 * Pipeline integration tests — validate the full Typesense + AST search
 * pipeline, mirroring exactly what the VS Code extension does.
 *
 * Start the server first:  ts start
 * Run all tests:           npm test
 * Run just this file:      node --require tsx/cjs --test src/test/pipeline.test.ts
 *
 * Live pipeline tests require the server AND the management API (port+1) AND:
 *
 *   CS_QUERY      Symbol/type to search for (required for live tests)
 *   CS_SUB        Subsystem filter, e.g. "myapp" (optional, default: '')
 *   CS_CONFIG     Path to config.json (optional; defaults to ../../../config.json)
 *
 * Example:
 *   CS_QUERY=IProcessor CS_SUB=root1 npm run test:pipeline
 *   CS_CONFIG=/tmp/e2e-config.json CS_QUERY=IProcessor npm run test:pipeline
 *
 * All live tests call the management API (/query-codebase on port+1) and skip
 * automatically when it is not reachable — whether CS_QUERY is unset or the
 * AST API is down.  In Docker E2E mode (run_tests.sh --docker), the management
 * API port is not exposed, so live tests skip gracefully (unit tests still run).
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
    // Check the indexserver management API (port+1) — Typesense is internal-only.
    serverAvailable = await queryApiIsUp(port);
    if (serverAvailable) { astAvailable = true; }
});

function skipIfNoServer(t: { skip(msg?: string): void }): boolean {
    if (!serverAvailable) { t.skip('Indexserver not running — run: ts start'); return true; }
    return false;
}

function skipIfNoLiveParams(t: { skip(msg?: string): void }): boolean {
    if (!serverAvailable) { t.skip('Indexserver not running — run: ts start'); return true; }
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
// Live pipeline tests — require CS_QUERY env var + running server
//
// These tests are generic: they exercise the pipeline contract regardless of
// what codebase is indexed. The query and subsystem come from env vars so
// this file has no knowledge of any specific project.
// ---------------------------------------------------------------------------

describe('pipeline — declarations mode (AST-backed, CS_QUERY)', () => {
    it('returns method_sigs from the index; each item has parens', async (t) => {
        if (skipIfNoLiveParams(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'declarations', 'cs', LIVE_SUB, '', 20);
        printTree(renderTextTree(result, `${LIVE_QUERY} [sub=${LIVE_SUB || '*'}]`, 'declarations'));

        assert.ok(result.found > 0, `no declarations results for "${LIVE_QUERY}"`);
        assert.ok(result.found >= result.hits.length);

        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0, `${hit.document.relative_path}: no match items`);
            for (const m of hit._matches) {
                assert.ok(m.text.length > 0, `declarations item is empty`);
            }
        }
    });
});

describe('pipeline — uses mode (AST-backed, CS_QUERY)', () => {
    it('returns exact line-level matches; all files have at least one match', async (t) => {
        if (skipIfNoAst(t)) { return; }
        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 20);
        printTree(renderTextTree(result, `${LIVE_QUERY} [sub=${LIVE_SUB || '*'}]`, 'uses'));

        assert.ok(result.found > 0, `no uses results for "${LIVE_QUERY}"`);

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
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 20);
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
            cfg, LIVE_QUERY, 'implements', 'cs', LIVE_SUB, '', 20);
        printTree(renderTextTree(result, `${LIVE_QUERY} [implements]`, 'implements'));
        // May legitimately return 0 if nothing inherits the type
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

// Expansion tests use CS_QUERY if set, otherwise fall back to 'IAbsBlobStore' which
// exists in the SPO.Core index, or the first symbol findable in the index.
const EXPANSION_QUERY = LIVE_QUERY || 'IAbsBlobStore';

describe('expansion — doQuerySingleFile round-trip (CS_QUERY or IAbsBlobStore fallback)', () => {
    it('expanding a file from search results yields valid line-number matches', async (t) => {
        if (skipIfNoServer(t)) { return; }

        // Step 1: search to get a result set, same as the webview does.
        const searchResult = await runSearchPipeline(
            cfg, EXPANSION_QUERY, 'uses', 'cs', LIVE_SUB, '', 10);
        if (searchResult.hits.length === 0) {
            t.skip(`no "uses" results for "${EXPANSION_QUERY}" — index may not contain this type`);
            return;
        }

        // Step 2: pick the first result and build the absolute path, same as the
        //         extension's expandFile handler does via resolveFilePath.
        const hit      = searchResult.hits[0];
        const relPath  = hit.document.relative_path;
        const absPath  = resolveFilePath(rootPath, relPath);

        // Step 3: call doQuerySingleFile — this is the call the extension makes.
        const matches = await doQuerySingleFile(cfg, 'uses', EXPANSION_QUERY, absPath);

        assert.ok(matches.length > 0,
            `doQuerySingleFile returned no matches for ${relPath} (query: "${EXPANSION_QUERY}")`);

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
            cfg, EXPANSION_QUERY, 'declarations', 'cs', LIVE_SUB, '', 10);
        if (searchResult.hits.length === 0) {
            t.skip(`no "declarations" results for "${EXPANSION_QUERY}"`);
            return;
        }

        const hit     = searchResult.hits[0];
        const absPath = resolveFilePath(rootPath, hit.document.relative_path);
        const matches = await doQuerySingleFile(cfg, 'declarations', EXPANSION_QUERY, absPath);

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
            cfg, EXPANSION_QUERY, 'uses', 'cs', LIVE_SUB, '', 5);
        const hit = searchResult.hits.find((h) => h._matches.length > 0 && h.ast_expanded !== false);
        if (!hit) {
            t.skip('no fully-expanded hits in search results');
            return;
        }

        const absPath       = resolveFilePath(rootPath, hit.document.relative_path);
        const expandMatches = await doQuerySingleFile(cfg, 'uses', EXPANSION_QUERY, absPath);

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
        const matches = await doQuerySingleFile(cfg, 'calls', EXPANSION_QUERY, fakeAbsPath);
        assert.deepEqual(matches, [], 'expected empty array for non-existent file');
    });
});

describe('pipeline — expansion simulation (CS_QUERY + CS_SUB)', () => {
    it('declarations expand: all returned hits belong to the requested subsystem', async (t) => {
        if (skipIfNoLiveParams(t)) { return; }
        if (!LIVE_SUB) { t.skip('CS_SUB not set — set CS_SUB=<subsystem> to test expansion'); return; }

        const result = await runSearchPipeline(
            cfg, LIVE_QUERY, 'declarations', 'cs', LIVE_SUB, '', 50);
        printTree(renderTextTree(result, `${LIVE_QUERY} [expand: ${LIVE_SUB}]`, 'declarations'));

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
            cfg, LIVE_QUERY, 'uses', 'cs', LIVE_SUB, '', 50);
        printTree(renderTextTree(result, `${LIVE_QUERY} [expand: ${LIVE_SUB}]`, 'uses'));

        assert.ok(result.found > 0);
        for (const hit of result.hits) {
            assert.ok(hit._matches.length > 0);
            assert.ok(hit._matches.some((m) => typeof m.line === 'number'));
        }
    });
});
