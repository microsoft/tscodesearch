/**
 * Integration tests — require a running Typesense server.
 * All tests skip automatically when the server is not reachable.
 *
 * Start the server first:  ts start
 *
 * What this tests:
 *   - Creates an isolated test collection with the same schema the indexer uses
 *   - Upserts a realistic C# document (WidgetService.cs with full metadata)
 *   - Polls GET /collections/{col} until num_documents >= 1, then waits for the
 *     document to appear in search results (mirrors the real indexer pipeline)
 *   - Exercises every search mode: text, symbols, implements, callers, sig, uses, attr
 *   - Verifies extension and subsystem filters
 *   - Cleans up the test collection after all tests, even on failure
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import * as http from 'node:http';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { tsSearch, buildSearchParams } from '../client';

// ---------------------------------------------------------------------------
// Read server config from the real codesearch/config.json
// ---------------------------------------------------------------------------

function readServerConfig(): { port: number; apiKey: string } {
    try {
        // __dirname is src/test/ → ../../../ is codesearch/
        const p = path.resolve(__dirname, '../../../config.json');
        if (fs.existsSync(p)) {
            const cfg = JSON.parse(fs.readFileSync(p, 'utf-8'));
            return { port: cfg.port ?? 8108, apiKey: cfg.api_key ?? 'codesearch-local' };
        }
    } catch { /* use defaults */ }
    return { port: 8108, apiKey: 'codesearch-local' };
}

const { port: PORT, apiKey: API_KEY } = readServerConfig();

// Unique collection name so parallel test runs don't collide
const TEST_COLLECTION = `codesearch_tstest_${Date.now()}`;

// ---------------------------------------------------------------------------
// Minimal Typesense admin HTTP client
// ---------------------------------------------------------------------------

interface TsResponse { status: number; data: unknown }

function tsAdmin(method: string, urlPath: string, body?: object): Promise<TsResponse> {
    return new Promise((resolve, reject) => {
        const payload = body ? JSON.stringify(body) : undefined;
        const req = http.request(
            {
                hostname: 'localhost', port: PORT, path: urlPath, method,
                headers: {
                    'X-TYPESENSE-API-KEY': API_KEY,
                    'Content-Type': 'application/json',
                    ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
                },
            },
            (res) => {
                let raw = '';
                res.on('data', (c) => (raw += c));
                res.on('end', () => {
                    try { resolve({ status: res.statusCode!, data: JSON.parse(raw) }); }
                    catch { resolve({ status: res.statusCode!, data: raw }); }
                });
            }
        );
        req.setTimeout(5000, () => req.destroy(new Error('admin request timed out')));
        req.on('error', reject);
        if (payload) { req.write(payload); }
        req.end();
    });
}

async function serverIsUp(): Promise<boolean> {
    try {
        const { status, data } = await tsAdmin('GET', '/health');
        return status === 200 && (data as { ok?: boolean }).ok === true;
    } catch {
        return false;
    }
}

// ---------------------------------------------------------------------------
// Collection schema — mirrors indexer.py build_schema() exactly
// ---------------------------------------------------------------------------

const SCHEMA = {
    name: TEST_COLLECTION,
    fields: [
        { name: 'id',            type: 'string' },
        { name: 'relative_path', type: 'string' },
        { name: 'filename',      type: 'string' },
        { name: 'extension',     type: 'string',   facet: true },
        { name: 'subsystem',     type: 'string',   facet: true },
        { name: 'namespace',     type: 'string',   optional: true },
        { name: 'class_names',   type: 'string[]', optional: true },
        { name: 'method_names',  type: 'string[]', optional: true },
        { name: 'symbols',       type: 'string[]' },
        { name: 'content',       type: 'string' },
        { name: 'mtime',         type: 'int64' },
        { name: 'base_types',    type: 'string[]', optional: true },
        { name: 'call_sites',    type: 'string[]', optional: true },
        { name: 'method_sigs',   type: 'string[]', optional: true },
        { name: 'type_refs',     type: 'string[]', optional: true },
        { name: 'attributes',    type: 'string[]', optional: true, facet: true },
        { name: 'usings',        type: 'string[]', optional: true },
        { name: 'priority',      type: 'int32' },
    ],
    // Split on C# syntax chars so generic type args and parameter types are
    // individually searchable — mirrors indexer.py build_schema()
    token_separators: ['(', ')', '<', '>', '[', ']', ','],
};

// ---------------------------------------------------------------------------
// Test document — what the tree-sitter indexer would produce for this source:
//
//   using System;
//   using MyApp.Core;
//   namespace MyApp.Services {
//     [Authorize]
//     [Route("api/widgets")]
//     public class WidgetService : IWidgetService, IDisposable {
//         public async Task<Widget> GetWidgetAsync(int id) { ... }
//         public Widget CreateWidget(WidgetConfig config) { ... }
//         public void DeleteWidget(int id) { ... }
//         public void Dispose() { ... }
//     }
//   }
// ---------------------------------------------------------------------------

const TEST_DOC = {
    id: 'test-widget-service',
    relative_path: 'Services/WidgetService.cs',
    filename: 'WidgetService.cs',
    extension: 'cs',
    subsystem: 'services',
    namespace: 'MyApp.Services',
    class_names: ['WidgetService', 'WidgetServiceConfig'],
    method_names: ['GetWidgetAsync', 'CreateWidget', 'DeleteWidget', 'Dispose'],
    symbols: ['WidgetService', 'WidgetServiceConfig', 'GetWidgetAsync', 'CreateWidget', 'DeleteWidget', 'Dispose'],
    content: [
        'using System;',
        'using MyApp.Core;',
        'namespace MyApp.Services {',
        '    [Authorize]',
        '    [Route("api/widgets")]',
        '    public class WidgetService : IWidgetService, IDisposable {',
        '        public async Task<Widget> GetWidgetAsync(int id) { return await _repo.FindAsync(id); }',
        '        public Widget CreateWidget(WidgetConfig config) { return _factory.Build(config); }',
        '        public void DeleteWidget(int id) { _repo.Remove(id); }',
        '        public void Dispose() { _repo?.Dispose(); }',
        '    }',
        '}',
    ].join('\n'),
    mtime: Math.floor(Date.now() / 1000),
    base_types: ['IWidgetService', 'IDisposable'],
    call_sites: ['FindAsync', 'Build', 'Remove'],
    method_sigs: [
        'Task<Widget> GetWidgetAsync(int id)',
        'Widget CreateWidget(WidgetConfig config)',
        'void DeleteWidget(int id)',
        'void Dispose()',
    ],
    type_refs: ['Widget', 'WidgetConfig', 'IWidgetService', 'Task'],
    attributes: ['Authorize', 'Route'],
    usings: ['System', 'MyApp'],
    priority: 3,
};

// ---------------------------------------------------------------------------
// waitForIndexed — polls collection metadata + search until the document is live
//
// Typesense writes are synchronous, so in practice this completes in one pass.
// The polling loop mirrors how real code waits on the async indexer pipeline.
// ---------------------------------------------------------------------------

async function waitForIndexed(
    query: string,
    mode: string,
    { timeoutMs = 10_000, intervalMs = 150 } = {}
): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    let lastError = '';

    while (Date.now() < deadline) {
        // Step 1 — collection metadata must show at least 1 document
        const { data } = await tsAdmin('GET', `/collections/${TEST_COLLECTION}`);
        const numDocs = (data as Record<string, unknown>)['num_documents'] as number ?? 0;

        if (numDocs >= 1) {
            // Step 2 — document must appear in actual search results
            try {
                const params = buildSearchParams(query, mode, '', '', 5);
                const result = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION, params);
                if (result.found > 0) { return; }
                lastError = `collection has ${numDocs} doc(s) but search for "${query}" returned 0`;
            } catch (e) {
                lastError = String(e);
            }
        } else {
            lastError = `collection has 0 documents`;
        }

        await new Promise<void>((r) => setTimeout(r, intervalMs));
    }

    throw new Error(`waitForIndexed timed out after ${timeoutMs}ms — ${lastError}`);
}

// ---------------------------------------------------------------------------
// Suite setup / teardown
// ---------------------------------------------------------------------------

let available = false;

before(async () => {
    available = await serverIsUp();
    if (!available) { return; }

    // Create the isolated test collection
    const { status, data } = await tsAdmin('POST', '/collections', SCHEMA);
    if (status !== 201) {
        throw new Error(`Failed to create test collection (${status}): ${JSON.stringify(data)}`);
    }

    // Upsert the test document — simulates what the indexer produces
    const { status: us, data: ud } = await tsAdmin(
        'POST',
        `/collections/${TEST_COLLECTION}/documents?action=upsert`,
        TEST_DOC
    );
    if (us !== 200 && us !== 201) {
        throw new Error(`Failed to upsert document (${us}): ${JSON.stringify(ud)}`);
    }

    // Poll until the document is visible in search — mirrors the real indexer pipeline
    await waitForIndexed('WidgetService', 'text');
});

after(async () => {
    // Always clean up, even if tests fail
    if (!available) { return; }
    try { await tsAdmin('DELETE', `/collections/${TEST_COLLECTION}`); } catch { /* best effort */ }
});

// Helper: skip the test if Typesense is not running
function skipIfUnavailable(t: { skip(msg?: string): void }): boolean {
    if (!available) {
        t.skip('Typesense not running — start with: ts start');
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('integration — WidgetService.cs indexed in real Typesense', () => {

    // ── text mode ─────────────────────────────────────────────────────────────

    it('text mode: finds document by class name', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', '', '', 10));
        assert.ok(r.found > 0, 'expected a result');
        assert.equal(r.hits[0].document.relative_path, 'Services/WidgetService.cs');
        assert.ok(r.hits[0].document.class_names?.includes('WidgetService'));
    });

    it('text mode: finds document by method name in content', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('GetWidgetAsync', 'text', '', '', 10));
        assert.ok(r.found > 0);
        assert.equal(r.hits[0].document.filename, 'WidgetService.cs');
    });

    it('text mode: returns highlights', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', '', '', 10));
        assert.ok(r.hits[0].highlights && r.hits[0].highlights.length > 0, 'expected highlights');
    });

    // ── symbols mode ──────────────────────────────────────────────────────────

    it('symbols mode: finds document by method name', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('GetWidgetAsync', 'symbols', '', '', 10));
        assert.ok(r.found > 0, 'expected symbols hit');
        assert.ok(r.hits[0].document.method_names?.includes('GetWidgetAsync'));
    });

    it('symbols mode: finds document by class name', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetServiceConfig', 'symbols', '', '', 10));
        assert.ok(r.found > 0);
        assert.ok(r.hits[0].document.class_names?.includes('WidgetServiceConfig'));
    });

    // ── implements mode ───────────────────────────────────────────────────────

    it('implements mode: finds types implementing a named interface', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('IWidgetService', 'implements', '', '', 10));
        assert.ok(r.found > 0, 'expected implements hit for IWidgetService');
        assert.ok(r.hits[0].document.base_types?.includes('IWidgetService'));
    });

    it('implements mode: finds IDisposable implementors', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('IDisposable', 'implements', '', '', 10));
        assert.ok(r.found > 0);
        assert.ok(r.hits[0].document.base_types?.includes('IDisposable'));
    });

    // ── callers mode ──────────────────────────────────────────────────────────

    it('callers mode: finds files that call a given method', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('FindAsync', 'callers', '', '', 10));
        assert.ok(r.found > 0, 'expected callers hit for FindAsync');
        assert.ok(r.hits[0].document.call_sites?.includes('FindAsync'));
    });

    // ── sig mode ──────────────────────────────────────────────────────────────

    it('sig mode: finds methods by return type', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('Task', 'sig', '', '', 10));
        assert.ok(r.found > 0, 'expected sig hit for Task return type');
        const sigs = r.hits[0].document.method_sigs ?? [];
        assert.ok(sigs.some((s) => s.includes('GetWidgetAsync')), `expected GetWidgetAsync in sigs: ${sigs}`);
    });

    it('sig mode: finds methods by return type (void)', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('void', 'sig', '', '', 10));
        assert.ok(r.found > 0, 'expected sig hit for void return type');
        const sigs = r.hits[0].document.method_sigs ?? [];
        assert.ok(sigs.some((s) => s.startsWith('void')), `expected a void sig: ${sigs}`);
    });

    it('sig mode: finds methods by generic return type (Widget inside Task<Widget>)', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        // token_separators splits "Task<Widget>" → "Task", "Widget"
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('Widget', 'sig', '', '', 10));
        assert.ok(r.found > 0, 'expected sig hit — Widget should be a token inside Task<Widget>');
        const sigs = r.hits[0].document.method_sigs ?? [];
        assert.ok(sigs.some((s) => s.includes('Widget')), `expected Widget in a sig: ${sigs}`);
    });

    it('sig mode: finds methods by parameter type (int)', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        // token_separators splits "GetWidgetAsync(int id)" → "GetWidgetAsync", "int", "id"
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('int', 'sig', '', '', 10));
        assert.ok(r.found > 0, 'expected sig hit — int should be a token inside (int id)');
        const sigs = r.hits[0].document.method_sigs ?? [];
        assert.ok(sigs.some((s) => s.includes('int id')), `expected "int id" in a sig: ${sigs}`);
    });

    it('sig mode: finds methods by parameter type (WidgetConfig)', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        // token_separators splits "CreateWidget(WidgetConfig config)" → "WidgetConfig", "config"
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetConfig', 'sig', '', '', 10));
        assert.ok(r.found > 0, 'expected sig hit — WidgetConfig should be a token inside (WidgetConfig config)');
        const sigs = r.hits[0].document.method_sigs ?? [];
        assert.ok(sigs.some((s) => s.includes('WidgetConfig')), `expected WidgetConfig in a sig: ${sigs}`);
    });

    // ── uses mode ─────────────────────────────────────────────────────────────

    it('uses mode: finds files that reference a type', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('Widget', 'uses', '', '', 10));
        assert.ok(r.found > 0, 'expected uses hit for Widget type');
        assert.ok(r.hits[0].document.type_refs?.includes('Widget'));
    });

    it('uses mode: finds files that reference IWidgetService', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('IWidgetService', 'uses', '', '', 10));
        assert.ok(r.found > 0);
        assert.ok(r.hits[0].document.type_refs?.includes('IWidgetService'));
    });

    // ── attr mode ─────────────────────────────────────────────────────────────

    it('attr mode: finds files decorated with a specific attribute', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('Authorize', 'attr', '', '', 10));
        assert.ok(r.found > 0, 'expected attr hit for Authorize');
        assert.ok(r.hits[0].document.attributes?.includes('Authorize'));
    });

    it('attr mode: finds files decorated with Route', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('Route', 'attr', '', '', 10));
        assert.ok(r.found > 0);
        assert.ok(r.hits[0].document.attributes?.includes('Route'));
    });

    // ── extension filter ──────────────────────────────────────────────────────

    it('extension filter: cs matches the document', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', 'cs', '', 10));
        assert.ok(r.found > 0);
        assert.equal(r.hits[0].document.extension, 'cs');
    });

    it('extension filter: non-matching extension returns no results', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', 'py', '', 10));
        assert.equal(r.found, 0, '.py filter should exclude the .cs document');
    });

    // ── subsystem filter ──────────────────────────────────────────────────────

    it('subsystem filter: correct subsystem returns results', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', '', 'services', 10));
        assert.ok(r.found > 0);
        assert.equal(r.hits[0].document.subsystem, 'services');
    });

    it('subsystem filter: wrong subsystem returns no results', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', '', 'storage', 10));
        assert.equal(r.found, 0, 'storage filter should exclude services document');
    });

    // ── result shape ──────────────────────────────────────────────────────────

    it('result document contains all expected metadata fields', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('WidgetService', 'text', '', '', 10));
        const doc = r.hits[0].document;
        assert.equal(doc.relative_path,  'Services/WidgetService.cs');
        assert.equal(doc.filename,        'WidgetService.cs');
        assert.equal(doc.extension,       'cs');
        assert.equal(doc.subsystem,       'services');
        assert.equal(doc.namespace,       'MyApp.Services');
        assert.deepEqual(doc.base_types,  ['IWidgetService', 'IDisposable']);
        assert.deepEqual(doc.attributes,  ['Authorize', 'Route']);
        assert.ok(doc.method_sigs && doc.method_sigs.length === 4, `expected 4 sigs, got ${doc.method_sigs?.length}`);
    });

    // ── no results ────────────────────────────────────────────────────────────

    it('returns zero results for an unindexed term', async (t) => {
        if (skipIfUnavailable(t)) { return; }
        const r = await tsSearch('localhost', PORT, API_KEY, TEST_COLLECTION,
            buildSearchParams('zzz_totally_nonexistent_xyzzy', 'text', '', '', 10));
        assert.equal(r.found, 0);
        assert.equal(r.hits.length, 0);
    });
});
