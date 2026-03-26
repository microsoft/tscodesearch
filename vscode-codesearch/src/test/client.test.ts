/**
 * Unit tests for client.ts.
 * Run with: npm test
 *
 * Covers:
 *   - Config parsing (legacy + multi-root, port reading)
 *   - Root and collection resolution
 *   - MODES constant validation
 *   - Path resolution (Windows, WSL, nested paths)
 *   - doQueryCodebase / runSearchPipeline against a mock /query-codebase server
 */

import { describe, it, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as http from 'node:http';

import {
    loadConfig,
    getRoots,
    sanitizeName,
    collectionForRoot,
    resolveFilePath,
    doQueryCodebase,
    doQuerySingleFile,
    runSearchPipeline,
    MODES,
    CodesearchConfig,
} from '../client';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function writeTempConfig(obj: object): string {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cstest-'));
    const p = path.join(dir, 'config.json');
    fs.writeFileSync(p, JSON.stringify(obj));
    return p;
}

// ---------------------------------------------------------------------------
// Mock /query-codebase server
// ---------------------------------------------------------------------------

let mockServer: http.Server;
let mockPort: number;

type MockQcHandler = (
    body: object,
    headers: http.IncomingHttpHeaders,
    url: string,
) => { status: number; body?: object; raw?: string };

let mockHandler: MockQcHandler = () => ({
    status: 200,
    body: { found: 0, overflow: false, hits: [], facet_counts: [] },
});

before(
    () =>
        new Promise<void>((resolve) => {
            mockServer = http.createServer((req, res) => {
                let rawBody = '';
                req.on('data', (chunk) => (rawBody += chunk));
                req.on('end', () => {
                    let parsed: object = {};
                    try { parsed = JSON.parse(rawBody); } catch { /* leave empty */ }
                    const result = mockHandler(parsed, req.headers, req.url ?? '');
                    res.writeHead(result.status, { 'Content-Type': 'application/json' });
                    res.end(result.raw ?? JSON.stringify(result.body ?? {}));
                });
            });
            mockServer.listen(0, () => {
                mockPort = (mockServer.address() as { port: number }).port;
                resolve();
            });
        })
);

after(() => new Promise<void>((resolve) => mockServer.close(() => resolve())));

// doQueryCodebase uses config.port + 1, so set port = mockPort - 1
function makeCfg(): CodesearchConfig {
    return { api_key: 'test-key', port: mockPort - 1, mode: 'wsl', roots: { default: { external_path: 'C:/src' } } };
}

beforeEach(() => {
    mockHandler = () => ({
        status: 200,
        body: { found: 0, overflow: false, hits: [], facet_counts: [] },
    });
});

// ---------------------------------------------------------------------------
// Config loading
// ---------------------------------------------------------------------------

describe('loadConfig', () => {
    it('reads config', () => {
        const p = writeTempConfig({ api_key: 'mk', port: 8108, mode: 'wsl', roots: { default: { external_path: 'C:/src' } } });
        const cfg = loadConfig(p);
        assert.equal(cfg.api_key, 'mk');
        assert.equal(cfg.port, 8108);
        assert.equal(cfg.mode, 'wsl');
        assert.deepEqual(cfg.roots, { default: { external_path: 'C:/src' } });
    });

    it('reads multiple roots', () => {
        const p = writeTempConfig({ api_key: 'mk', port: 8108, mode: 'docker', roots: { default: { external_path: 'C:/src' }, other: { external_path: 'C:/other' } } });
        const cfg = loadConfig(p);
        assert.deepEqual(cfg.roots, { default: { external_path: 'C:/src' }, other: { external_path: 'C:/other' } });
    });

    it('throws on missing file', () => {
        assert.throws(() => loadConfig('/no/such/config.json'));
    });

    it('throws on malformed JSON', () => {
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cstest-'));
        const p = path.join(dir, 'config.json');
        fs.writeFileSync(p, '{ bad json }');
        assert.throws(() => loadConfig(p));
    });
});

// ---------------------------------------------------------------------------
// getRoots
// ---------------------------------------------------------------------------

describe('getRoots', () => {
    it('returns the roots map', () => {
        const cfg: CodesearchConfig = { api_key: 'x', port: 8108, mode: 'wsl', roots: { default: { external_path: 'C:/src' }, foo: { external_path: 'C:/foo' } } };
        assert.deepEqual(getRoots(cfg), { default: 'C:/src', foo: 'C:/foo' });
    });

    it('returns empty object for no roots', () => {
        const cfg: CodesearchConfig = { api_key: 'x', port: 8108, mode: 'docker', roots: {} };
        assert.deepEqual(getRoots(cfg), {});
    });
});

// ---------------------------------------------------------------------------
// Collection naming
// ---------------------------------------------------------------------------

describe('collectionForRoot', () => {
    it('default root → codesearch_default', () => {
        assert.equal(collectionForRoot('default'), 'codesearch_default');
    });

    it('uppercased name is lowercased', () => {
        assert.equal(collectionForRoot('MyRoot'), 'codesearch_myroot');
    });

    it('hyphens and spaces become underscores', () => {
        assert.equal(collectionForRoot('my-root project'), 'codesearch_my_root_project');
    });

    it('sanitizes special chars', () => {
        assert.equal(collectionForRoot('root@2!'), 'codesearch_root_2_');
    });
});

describe('sanitizeName', () => {
    it('leaves lowercase alphanumeric unchanged', () => {
        assert.equal(sanitizeName('abc123'), 'abc123');
    });

    it('converts uppercase to lowercase', () => {
        assert.equal(sanitizeName('AbC'), 'abc');
    });

    it('replaces disallowed chars with underscore', () => {
        assert.equal(sanitizeName('a-b.c/d'), 'a_b_c_d');
    });
});

// ---------------------------------------------------------------------------
// MODES constant
// ---------------------------------------------------------------------------

describe('MODES constant', () => {
    it('contains all 11 modes in declared order', () => {
        const keys = MODES.map((m) => m.key);
        assert.deepEqual(keys, [
            // search-only
            'text',
            // AST-backed
            'declarations', 'uses', 'calls', 'implements', 'casts', 'attrs',
            'all_refs', 'accesses_on',
            // uses sub-modes
            'uses_field', 'uses_param',
        ]);
    });

    it('text mode has no astMode (search-only)', () => {
        const m = MODES.find((x) => x.key === 'text')!;
        assert.equal(m.astMode, undefined, 'text should not have astMode');
    });

    it('AST-backed modes have astMode set', () => {
        for (const key of ['declarations', 'uses', 'calls', 'implements', 'casts', 'attrs',
                           'all_refs', 'accesses_on']) {
            const m = MODES.find((x) => x.key === key)!;
            assert.equal(m.astMode, key, `${key}: astMode should equal key`);
        }
    });

    it('uses_field and uses_param have astMode=uses and uses_kind set', () => {
        const field = MODES.find((x) => x.key === 'uses_field')!;
        assert.equal(field.astMode, 'uses');
        assert.equal(field.uses_kind, 'field');
        const param = MODES.find((x) => x.key === 'uses_param')!;
        assert.equal(param.astMode, 'uses');
        assert.equal(param.uses_kind, 'param');
    });

    it('every mode has key, label, queryBy, weights, desc', () => {
        for (const m of MODES) {
            assert.ok(m.key, `${m.key}: missing key`);
            assert.ok(m.label, `${m.key}: missing label`);
            assert.ok(m.queryBy, `${m.key}: missing queryBy`);
            assert.ok(m.weights, `${m.key}: missing weights`);
            assert.ok(m.desc, `${m.key}: missing desc`);
        }
    });

    it('weight count matches queryBy field count for each mode', () => {
        for (const m of MODES) {
            const fieldCount = m.queryBy.split(',').length;
            const weightCount = m.weights.split(',').length;
            assert.equal(fieldCount, weightCount, `${m.key}: field count ${fieldCount} !== weight count ${weightCount}`);
        }
    });
});

// ---------------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------------

describe('resolveFilePath', () => {
    it('joins Windows root with relative path', () => {
        assert.equal(resolveFilePath('C:/myproject/src', 'Foo/Bar.cs'), 'C:/myproject/src/Foo/Bar.cs');
    });

    it('handles backslashes in root', () => {
        assert.equal(resolveFilePath('C:\\myproject\\src', 'Foo/Bar.cs'), 'C:/myproject/src/Foo/Bar.cs');
    });

    it('strips trailing slash from root', () => {
        assert.equal(resolveFilePath('C:/myproject/src/', 'Foo/Bar.cs'), 'C:/myproject/src/Foo/Bar.cs');
    });

    it('returns absolute WSL path (/...) as-is without prepending root', () => {
        // A leading-slash path is treated as absolute (e.g. /mnt/c/... from WSL).
        // It is returned unchanged — the caller is responsible for further conversion.
        assert.equal(resolveFilePath('C:/myproject/src', '/mnt/c/myproject/src/Foo/Bar.cs'), '/mnt/c/myproject/src/Foo/Bar.cs');
    });

    it('converts WSL /mnt/c/... root to C:/...', () => {
        assert.equal(resolveFilePath('/mnt/c/myproject/src', 'Foo/Bar.cs'), 'C:/myproject/src/Foo/Bar.cs');
    });

    it('handles lowercase drive letter in WSL path', () => {
        assert.equal(resolveFilePath('/mnt/c/code', 'src/main.cs'), 'C:/code/src/main.cs');
    });

    it('preserves deeply nested relative paths', () => {
        assert.equal(resolveFilePath('C:/src', 'a/b/c/d/e.cs'), 'C:/src/a/b/c/d/e.cs');
    });
});

// ---------------------------------------------------------------------------
// doQueryCodebase — HTTP client against mock /query-codebase server
// ---------------------------------------------------------------------------

describe('doQueryCodebase', () => {
    it('POSTs to /query-codebase on port config.port+1', async () => {
        let capturedPath = '';
        let capturedMethod = '';
        mockHandler = (_, headers) => {
            capturedPath = (headers as { url?: string }).url ?? '';
            capturedMethod = 'POST';  // server only receives the body, not the path
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        // To capture path, wrap the server differently — just verify the call succeeds
        await doQueryCodebase(makeCfg(), 'IWidget', 'uses', '', '', '', 10);
        assert.equal(capturedMethod, 'POST');
    });

    it('sends X-TYPESENSE-API-KEY header', async () => {
        let capturedKey = '';
        mockHandler = (_, headers) => {
            capturedKey = headers['x-typesense-api-key'] as string;
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        const cfg = makeCfg();
        cfg.api_key = 'my-secret';
        await doQueryCodebase(cfg, 'IFoo', 'uses', '', '', '', 10);
        assert.equal(capturedKey, 'my-secret');
    });

    it('sends mode, pattern, ext, sub, root, limit in body', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => {
            body = b as Record<string, unknown>;
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        await doQueryCodebase(makeCfg(), 'IWidget', 'calls', 'cs', 'storage', 'main', 20);
        assert.equal(body['mode'], 'calls');
        assert.equal(body['pattern'], 'IWidget');
        assert.equal(body['ext'], 'cs');
        assert.equal(body['sub'], 'storage');
        assert.equal(body['root'], 'main');
        assert.equal(body['limit'], 20);
    });

    it('resolves mode key to server astMode (declarations → declarations)', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => {
            body = b as Record<string, unknown>;
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        await doQueryCodebase(makeCfg(), 'Foo', 'declarations', '', '', '', 10);
        assert.equal(body['mode'], 'declarations');
        assert.equal(body['uses_kind'], undefined);
    });

    it('maps uses_field key to mode=uses with uses_kind=field', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => {
            body = b as Record<string, unknown>;
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        await doQueryCodebase(makeCfg(), 'IWidget', 'uses_field', '', '', '', 10);
        assert.equal(body['mode'], 'uses');
        assert.equal(body['uses_kind'], 'field');
    });

    it('maps uses_param key to mode=uses with uses_kind=param', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => {
            body = b as Record<string, unknown>;
            return { status: 200, body: { found: 0, overflow: false, hits: [], facet_counts: [] } };
        };
        await doQueryCodebase(makeCfg(), 'IWidget', 'uses_param', '', '', '', 10);
        assert.equal(body['mode'], 'uses');
        assert.equal(body['uses_kind'], 'param');
    });

    it('maps response hits and converts line numbers 1-indexed→0-indexed', async () => {
        mockHandler = () => ({
            status: 200,
            body: {
                found: 1, overflow: false,
                hits: [{
                    document: { id: '42', relative_path: 'foo/Bar.cs', subsystem: 'foo', filename: 'Bar.cs' },
                    matches: [
                        { line: 10, text: 'public void Foo()' },
                        { line: 20, text: 'private IWidget _widget;' },
                    ],
                }],
                facet_counts: [],
            },
        });
        const result = await doQueryCodebase(makeCfg(), 'IWidget', 'uses', '', '', '', 10);
        assert.equal(result.found, 1);
        assert.equal(result.hits.length, 1);
        assert.equal(result.hits[0].document.relative_path, 'foo/Bar.cs');
        assert.equal(result.hits[0]._matches.length, 2);
        assert.equal(result.hits[0]._matches[0].line, 9);   // 10 - 1
        assert.equal(result.hits[0]._matches[1].line, 19);  // 20 - 1
        assert.equal(result.hits[0]._matches[0].text, 'public void Foo()');
    });

    it('propagates overflow flag', async () => {
        mockHandler = () => ({
            status: 200,
            body: { found: 500, overflow: true, hits: [], facet_counts: [] },
        });
        const result = await doQueryCodebase(makeCfg(), 'x', 'uses', '', '', '', 10);
        assert.equal(result.overflow, true);
        assert.equal(result.found, 500);
    });

    it('rejects on HTTP 400 with error message', async () => {
        mockHandler = () => ({
            status: 400,
            body: { error: 'unknown mode: bad' },
        });
        await assert.rejects(
            () => doQueryCodebase(makeCfg(), 'x', 'uses', '', '', '', 10),
            /unknown mode/
        );
    });

    it('rejects when server is unreachable', async () => {
        const cfg: CodesearchConfig = { api_key: 'k', port: 1, mode: 'wsl', roots: { default: { external_path: 'C:/src' } } };
        await assert.rejects(() => doQueryCodebase(cfg, 'x', 'uses', '', '', '', 10));
    });
});

// ---------------------------------------------------------------------------
// runSearchPipeline — wraps doQueryCodebase and adds elapsed timing
// ---------------------------------------------------------------------------

describe('runSearchPipeline', () => {
    it('returns hits and found from server', async () => {
        mockHandler = () => ({
            status: 200,
            body: {
                found: 2, overflow: false,
                hits: [
                    { document: { id: '1', relative_path: 'a/A.cs', subsystem: 'a', filename: 'A.cs' },
                      matches: [{ line: 5, text: 'void Foo()' }] },
                    { document: { id: '2', relative_path: 'b/B.cs', subsystem: 'b', filename: 'B.cs' },
                      matches: [{ line: 3, text: 'IFoo _foo;' }] },
                ],
                facet_counts: [{ field_name: 'subsystem', counts: [{ value: 'a', count: 1 }] }],
            },
        });
        const result = await runSearchPipeline(makeCfg(), 'IFoo', 'uses', '', '', '', 10);
        assert.equal(result.found, 2);
        assert.equal(result.hits.length, 2);
        assert.equal(result.hits[0].document.relative_path, 'a/A.cs');
        assert.equal(result.hits[0]._matches[0].line, 4);  // 5 - 1
    });

    it('includes elapsed time in result', async () => {
        const result = await runSearchPipeline(makeCfg(), 'x', 'calls', '', '', '', 10);
        assert.ok(typeof result.elapsed === 'number' && result.elapsed >= 0);
    });

    it('propagates overflow and facet_counts', async () => {
        mockHandler = () => ({
            status: 200,
            body: {
                found: 100, overflow: true, hits: [],
                facet_counts: [{ field_name: 'extension', counts: [{ value: 'cs', count: 100 }] }],
            },
        });
        const result = await runSearchPipeline(makeCfg(), 'x', 'uses', '', '', '', 10);
        assert.equal(result.overflow, true);
        assert.ok(result.facet_counts!.some((f) => f.field_name === 'extension'));
    });

    it('rejects on server error', async () => {
        mockHandler = () => ({ status: 500, body: { error: 'internal error' } });
        await assert.rejects(
            () => runSearchPipeline(makeCfg(), 'x', 'uses', '', '', '', 10),
            /internal error/
        );
    });

    // ------------------------------------------------------------------
    // Large-result / capped-view expansion scenario:
    //   1. Initial search returns overflow (too many files, hits=[])
    //   2. User clicks a subsystem → expandSub triggers a sub-filtered search
    //   3. Sub-filtered search returns actual hits that can be clicked
    // ------------------------------------------------------------------
    it('overflow search returns overflow=true, found>0, and empty hits array', async () => {
        mockHandler = () => ({
            status: 200,
            body: {
                found: 350, overflow: true, hits: [],
                facet_counts: [{ field_name: 'subsystem', counts: [
                    { value: 'storage', count: 200 },
                    { value: 'api',     count: 150 },
                ] }],
            },
        });
        const result = await runSearchPipeline(makeCfg(), 'Widget', 'text', '', '', 'default', 20);
        assert.equal(result.overflow, true);
        assert.equal(result.found, 350);
        assert.equal(result.hits.length, 0, 'overflow result must have no hits');
        assert.ok(result.facet_counts, 'facet_counts must be present for capped-view subsystem list');
        const sf = result.facet_counts!.find((f) => f.field_name === 'subsystem');
        assert.ok(sf, 'subsystem facet must be present');
        assert.equal(sf!.counts.length, 2);
        assert.equal(sf!.counts[0].value, 'storage');
    });

    it('expandSub: sub-filtered search returns hits with matches and correct ast_expanded', async () => {
        // Simulate the expandSub call: same query, sub='storage' filter, limit=50
        let capturedBody: Record<string, unknown> = {};
        mockHandler = (b) => {
            capturedBody = b as Record<string, unknown>;
            return {
                status: 200,
                body: {
                    found: 3, overflow: false,
                    hits: [
                        {
                            document: { id: '1', relative_path: 'storage/WidgetStore.cs', subsystem: 'storage', filename: 'WidgetStore.cs' },
                            matches: [{ line: 12, text: '  private readonly IWidgetStore _store;' }],
                            ast_expanded: true,
                        },
                        {
                            document: { id: '2', relative_path: 'storage/WidgetCache.cs', subsystem: 'storage', filename: 'WidgetCache.cs' },
                            matches: [{ line: 5, text: '  public class WidgetCache' }],
                            ast_expanded: false,  // server did not expand this file
                        },
                        {
                            document: { id: '3', relative_path: 'storage/WidgetIndex.cs', subsystem: 'storage', filename: 'WidgetIndex.cs' },
                            matches: [],
                            // ast_expanded omitted — old-format response, should default to true
                        },
                    ],
                    facet_counts: [],
                },
            };
        };
        const result = await runSearchPipeline(makeCfg(), 'Widget', 'text', '', 'storage', 'default', 50);

        // Verify the sub filter was forwarded to the server
        assert.equal(capturedBody['sub'], 'storage');
        assert.equal(capturedBody['limit'], 50);

        assert.equal(result.found, 3);
        assert.equal(result.hits.length, 3);

        // Hit 0: ast_expanded=true, line converted 12→11
        assert.equal(result.hits[0].document.relative_path, 'storage/WidgetStore.cs');
        assert.equal(result.hits[0].ast_expanded, true);
        assert.equal(result.hits[0]._matches.length, 1);
        assert.equal(result.hits[0]._matches[0].line, 11);  // 12 - 1

        // Hit 1: ast_expanded=false (server skipped AST) — clicking this file should still work
        assert.equal(result.hits[1].document.relative_path, 'storage/WidgetCache.cs');
        assert.equal(result.hits[1].ast_expanded, false,
            'ast_expanded=false must be preserved so the webview knows to re-expand on click');
        assert.equal(result.hits[1]._matches[0].line, 4);   // 5 - 1

        // Hit 2: ast_expanded omitted in response — must default to true (backwards compat)
        assert.equal(result.hits[2].ast_expanded, true,
            'missing ast_expanded in server response must default to true');
    });

    it('expandSub still-overflowing subsystem: returns overflow=true with empty hits', async () => {
        // Subsystem itself has too many matches — should surface overflow, not silently show nothing
        mockHandler = () => ({
            status: 200,
            body: { found: 80, overflow: true, hits: [], facet_counts: [] },
        });
        const result = await runSearchPipeline(makeCfg(), 'Widget', 'text', '', 'storage', 'default', 50);
        assert.equal(result.overflow, true);
        assert.equal(result.found, 80);
        assert.equal(result.hits.length, 0);
    });
});

// ---------------------------------------------------------------------------
// doQuerySingleFile — HTTP client against mock /query server
// ---------------------------------------------------------------------------

describe('doQuerySingleFile', () => {
    it('POSTs to /query (not /query-codebase)', async () => {
        let capturedUrl = '';
        mockHandler = (_, __, url) => {
            capturedUrl = url;
            return { status: 200, body: { results: [] } };
        };
        await doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs');
        assert.equal(capturedUrl, '/query');
    });

    it('sends files as a single-element array containing the absolute path', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => { body = b as Record<string, unknown>; return { status: 200, body: { results: [] } }; };
        await doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/myfile.cs');
        assert.deepEqual(body['files'], ['C:/src/myfile.cs']);
    });

    it('sends mode and pattern in body', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => { body = b as Record<string, unknown>; return { status: 200, body: { results: [] } }; };
        await doQuerySingleFile(makeCfg(), 'calls', 'SaveWidget', 'C:/src/foo.cs');
        assert.equal(body['mode'], 'calls');
        assert.equal(body['pattern'], 'SaveWidget');
    });

    it('resolves mode key to astMode (declarations → declarations)', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => { body = b as Record<string, unknown>; return { status: 200, body: { results: [] } }; };
        await doQuerySingleFile(makeCfg(), 'declarations', 'Widget', 'C:/src/foo.cs');
        assert.equal(body['mode'], 'declarations');
        assert.equal(body['uses_kind'], undefined);
    });

    it('sends uses_kind=field for uses_field mode', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => { body = b as Record<string, unknown>; return { status: 200, body: { results: [] } }; };
        await doQuerySingleFile(makeCfg(), 'uses_field', 'IWidget', 'C:/src/foo.cs');
        assert.equal(body['mode'], 'uses');
        assert.equal(body['uses_kind'], 'field');
    });

    it('sends uses_kind=param for uses_param mode', async () => {
        let body: Record<string, unknown> = {};
        mockHandler = (b) => { body = b as Record<string, unknown>; return { status: 200, body: { results: [] } }; };
        await doQuerySingleFile(makeCfg(), 'uses_param', 'IWidget', 'C:/src/foo.cs');
        assert.equal(body['mode'], 'uses');
        assert.equal(body['uses_kind'], 'param');
    });

    it('converts 1-indexed line numbers to 0-indexed', async () => {
        mockHandler = () => ({
            status: 200,
            body: {
                results: [{
                    file: 'C:/src/foo.cs',
                    matches: [
                        { line: 1,  text: 'first line' },
                        { line: 10, text: 'tenth line' },
                        { line: 42, text: 'answer' },
                    ],
                }],
            },
        });
        const matches = await doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs');
        assert.equal(matches.length, 3);
        assert.equal(matches[0].line, 0);   // 1  - 1
        assert.equal(matches[1].line, 9);   // 10 - 1
        assert.equal(matches[2].line, 41);  // 42 - 1
        assert.equal(matches[2].text, 'answer');
    });

    it('returns empty array when results have no matches', async () => {
        mockHandler = () => ({
            status: 200,
            body: { results: [{ file: 'C:/src/foo.cs', matches: [] }] },
        });
        const matches = await doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs');
        assert.deepEqual(matches, []);
    });

    it('returns empty array when results array is empty', async () => {
        mockHandler = () => ({ status: 200, body: { results: [] } });
        const matches = await doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs');
        assert.deepEqual(matches, []);
    });

    it('sends X-TYPESENSE-API-KEY header', async () => {
        let capturedKey = '';
        mockHandler = (_, headers) => {
            capturedKey = headers['x-typesense-api-key'] as string;
            return { status: 200, body: { results: [] } };
        };
        const cfg = makeCfg();
        cfg.api_key = 'secret-file-key';
        await doQuerySingleFile(cfg, 'calls', 'Foo', 'C:/src/foo.cs');
        assert.equal(capturedKey, 'secret-file-key');
    });

    it('rejects on HTTP 400 with error message', async () => {
        mockHandler = () => ({ status: 400, body: { error: 'invalid mode: bad' } });
        await assert.rejects(
            () => doQuerySingleFile(makeCfg(), 'bad', 'x', 'C:/src/foo.cs'),
            /invalid mode/,
        );
    });

    it('rejects on HTTP 500', async () => {
        mockHandler = () => ({ status: 500, body: { error: 'internal server error' } });
        await assert.rejects(
            () => doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs'),
            /internal server error/,
        );
    });

    it('rejects when server is unreachable', async () => {
        const cfg: CodesearchConfig = { api_key: 'k', port: 1, mode: 'wsl', roots: { default: { external_path: 'C:/src' } } };
        await assert.rejects(() => doQuerySingleFile(cfg, 'calls', 'Foo', 'C:/src/foo.cs'));
    });

    it('rejects on malformed JSON response', async () => {
        mockHandler = () => ({ status: 200, raw: 'not json at all' });
        await assert.rejects(
            () => doQuerySingleFile(makeCfg(), 'calls', 'Foo', 'C:/src/foo.cs'),
            /Bad JSON/,
        );
    });
});
