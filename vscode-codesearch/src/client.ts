/**
 * Pure business logic for the codesearch extension.
 * No vscode imports — safe to test in plain Node.js.
 */
import * as fs from 'fs';
import * as http from 'http';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CodesearchConfig {
    api_key: string;
    port?: number;
    roots?: Record<string, string>;
    src_root?: string; // legacy
}

export interface TypesenseHit {
    document: {
        id: string;
        relative_path: string;
        filename: string;
        extension?: string;
        subsystem?: string;
        namespace?: string;
        class_names?: string[];
        method_names?: string[];
        symbols?: string[];
        base_types?: string[];
        call_sites?: string[];
        cast_sites?: string[];
        method_sigs?: string[];
        type_refs?: string[];
        attributes?: string[];
        usings?: string[];
    };
    highlights?: Array<{
        field: string;
        snippet?: string;
        snippets?: string[];
        values?: string[];    // matched array element values (array fields)
        indices?: number[];   // positions in the source array that matched
    }>;
}

export interface TypesenseResult {
    found: number;
    hits: TypesenseHit[];
    facet_counts?: Array<{
        field_name: string;
        counts: Array<{ value: string; count: number }>;
    }>;
    search_time_ms?: number;
}

// ---------------------------------------------------------------------------
// Search modes
// ---------------------------------------------------------------------------

/**
 * Search modes.  All modes go through the server's /query-codebase endpoint,
 * which runs Typesense pre-filter + tree-sitter AST in one call and returns
 * exact line numbers for every result.
 *
 * astMode: kept for backwards compat; server owns the Typesense→AST mapping.
 */
export const MODES: Array<{
    key: string; label: string; queryBy: string; weights: string; desc: string;
    astMode?: string;
}> = [
    {
        key: 'text',
        label: 'Text',
        queryBy: 'filename,symbols,class_names,method_names,content',
        weights: '5,4,4,4,1',
        desc: 'Full-text search across filenames, symbols, and file content',
    },
    {
        key: 'symbols',
        label: 'Symbols',
        queryBy: 'symbols,class_names,method_names,filename',
        weights: '4,4,4,3',
        desc: 'Search only class/interface/method/property names [symbols]',
    },
    {
        key: 'sig',
        label: 'Signatures',
        queryBy: 'method_sigs,method_names,filename',
        weights: '4,3,2',
        desc: 'Search method signatures by return/parameter types [method_sigs]',
    },
    {
        key: 'uses',
        label: 'Uses',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Every type reference: declarations, locals, static receivers [type_refs]',
        astMode: 'uses',
    },
    {
        key: 'calls',
        label: 'Calls',
        queryBy: 'call_sites,filename',
        weights: '4,2',
        desc: 'Every call site of the given method [call_sites]',
        astMode: 'calls',
    },
    {
        key: 'implements',
        label: 'Implements',
        queryBy: 'base_types,class_names,filename',
        weights: '4,3,2',
        desc: 'Types that inherit or implement the given interface/class [base_types]',
        astMode: 'implements',
    },
    {
        key: 'casts',
        label: 'Casts',
        queryBy: 'cast_sites,filename',
        weights: '4,2',
        desc: 'Explicit (TYPE)expr cast expressions [cast_sites]',
        astMode: 'casts',
    },
    {
        key: 'attrs',
        label: 'Attrs',
        queryBy: 'attributes,filename',
        weights: '4,2',
        desc: 'Files decorated with the given [Attribute] [attributes]',
        astMode: 'attrs',
    },
    {
        key: 'field_type',
        label: 'Field Type',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Fields/properties declared with the given type [type_refs]',
        astMode: 'field_type',
    },
    {
        key: 'param_type',
        label: 'Param Type',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Method/constructor parameters typed as the given type [type_refs]',
        astMode: 'param_type',
    },
    {
        key: 'ident',
        label: 'Ident',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Every identifier occurrence (semantic grep, skips comments/strings) [type_refs]',
        astMode: 'ident',
    },
    {
        key: 'member_accesses',
        label: 'Members',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: '.Member accesses on locals/params of the given type [type_refs]',
        astMode: 'member_accesses',
    },
    {
        key: 'find',
        label: 'Find',
        queryBy: 'method_sigs,method_names,filename',
        weights: '4,3,2',
        desc: 'Full source body of the method/type/property named NAME [method_sigs]',
        astMode: 'find',
    },
    {
        key: 'params',
        label: 'Params',
        queryBy: 'method_sigs,method_names,filename',
        weights: '4,3,2',
        desc: 'Parameter list of the given method [method_sigs]',
        astMode: 'params',
    },
];

// ---------------------------------------------------------------------------
// Config helpers
// ---------------------------------------------------------------------------

export function loadConfig(configPath: string): CodesearchConfig {
    return JSON.parse(fs.readFileSync(configPath, 'utf-8'));
}

export function getRoots(config: CodesearchConfig): Record<string, string> {
    if (config.roots && Object.keys(config.roots).length > 0) { return config.roots; }
    if (config.src_root) { return { default: config.src_root }; }
    return {};
}

export function sanitizeName(name: string): string {
    return name.toLowerCase().replace(/[^a-z0-9_]/g, '_');
}

export function collectionForRoot(name: string): string {
    return `codesearch_${sanitizeName(name)}`;
}

// ---------------------------------------------------------------------------
// Search param builder (pure — no I/O)
// ---------------------------------------------------------------------------

export function buildSearchParams(
    query: string,
    modeKey: string,
    ext: string,
    sub: string,
    limit: number
): Record<string, string> {
    const mode = MODES.find((m) => m.key === modeKey) ?? MODES[0];

    const filterParts: string[] = [];
    if (ext) { filterParts.push(`extension:=${ext.replace(/^\./, '')}`); }
    if (sub) { filterParts.push(`subsystem:=${sub}`); }

    const params: Record<string, string> = {
        q: query,
        query_by: mode.queryBy,
        query_by_weights: mode.weights,
        per_page: String(limit),
        highlight_full_fields: 'filename,symbols,class_names,method_names,base_types,method_sigs,type_refs,call_sites,attributes',
        snippet_threshold: '30',
        num_typos: query.length < 4 ? '0' : '1',
        prefix: 'true',
        facet_by: 'subsystem,extension',
    };
    if (!ext) { params['sort_by'] = '_text_match:desc,priority:desc'; }
    if (filterParts.length) { params['filter_by'] = filterParts.join(' && '); }

    return params;
}

// ---------------------------------------------------------------------------
// Typesense HTTP client
// ---------------------------------------------------------------------------

export function tsSearch(
    host: string, port: number, apiKey: string,
    collection: string, params: Record<string, string>
): Promise<TypesenseResult> {
    return new Promise((resolve, reject) => {
        const qs = new URLSearchParams(params).toString();
        const req = http.request(
            {
                hostname: host,
                port,
                path: `/collections/${collection}/documents/search?${qs}`,
                method: 'GET',
                headers: { 'X-TYPESENSE-API-KEY': apiKey },
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    try {
                        const parsed = JSON.parse(data);
                        if (res.statusCode && res.statusCode >= 400) {
                            reject(new Error(`Typesense ${res.statusCode}: ${parsed.message ?? data.slice(0, 200)}`));
                        } else {
                            resolve(parsed);
                        }
                    } catch {
                        reject(new Error(`Bad JSON from Typesense: ${data.slice(0, 200)}`));
                    }
                });
            }
        );
        req.setTimeout(8000, () => req.destroy(new Error('Typesense request timed out')));
        req.on('error', reject);
        req.end();
    });
}

export async function doSearch(
    config: CodesearchConfig,
    query: string,
    modeKey: string,
    ext: string,
    sub: string,
    rootName: string,
    limit: number
): Promise<TypesenseResult> {
    const host = 'localhost';
    const port = config.port ?? 8108;
    const apiKey = config.api_key ?? 'codesearch-local';
    const roots = getRoots(config);
    const effectiveRoot = (rootName && roots[rootName]) ? rootName : Object.keys(roots)[0] ?? 'default';
    const collection = collectionForRoot(effectiveRoot);
    const params = buildSearchParams(query, modeKey, ext, sub, limit);
    return tsSearch(host, port, apiKey, collection, params);
}

// ---------------------------------------------------------------------------
// Tree-sitter query via indexserver HTTP API
// ---------------------------------------------------------------------------

export interface QueryMatch { line: number; text: string; }  // line is 1-indexed
export interface QueryFileResult { file: string; matches: QueryMatch[]; }

/** A single match item displayed under a file node in the results tree. */
export interface MatchItem { text: string; line?: number; }  // line is 0-indexed

/**
 * Compute display match items from a Typesense hit (highlights-based).
 * Used as a utility/fallback; live pipeline gets matches from the server's AST.
 */
export function computeMatchItems(hit: TypesenseHit, mode: string): MatchItem[] {
    const doc = hit.document;
    const hl = hit.highlights ?? [];
    switch (mode) {
        case 'text': {
            for (const h of hl) {
                if (h.field === 'content') {
                    const s = h.snippet ?? h.snippets?.[0];
                    if (s) { return [{ text: s.replace(/<\/?mark>/g, '').trim() }]; }
                }
            }
            return (doc.method_names ?? []).slice(0, 6).map((n) => ({ text: n }));
        }
        case 'symbols':
            return [
                ...(doc.class_names ?? []).slice(0, 3).map((n) => ({ text: n })),
                ...(doc.method_names ?? []).slice(0, 6).map((n) => ({ text: n })),
            ].slice(0, 8);
        case 'implements': {
            const h = hl.find((e) => e.field === 'base_types');
            return (h?.values ?? doc.base_types ?? []).map((t) => ({ text: t }));
        }
        case 'sig':
        case 'find':
        case 'params': {
            // Use the Typesense highlight values — these are the specific sigs that
            // matched the query.  A file only appears in results because method_sigs
            // matched, so highlights are always present.
            const h = hl.find((e) => e.field === 'method_sigs');
            return (h?.values ?? []).map((s) => ({ text: s }));
        }
        case 'uses':
        case 'field_type':
        case 'param_type':
        case 'ident':
        case 'member_accesses': {
            const h = hl.find((e) => e.field === 'type_refs');
            return (h?.values ?? doc.type_refs ?? []).slice(0, 8).map((t) => ({ text: t }));
        }
        case 'attrs': {
            const h = hl.find((e) => e.field === 'attributes');
            return (h?.values ?? doc.attributes ?? []).map((a) => ({ text: a }));
        }
        case 'casts': {
            const h = hl.find((e) => e.field === 'cast_sites');
            return (h?.values ?? doc.cast_sites ?? []).map((c) => ({ text: c }));
        }
        default:
            return [];
    }
}

/**
 * Valid tree-sitter query modes — mirrors the modes accepted by
 * query_codebase and query_single_file in mcp_server.py.
 */
export const QUERY_MODES = [
    // C# — pattern modes (query_codebase + query_single_file)
    'uses', 'calls', 'implements', 'casts', 'field_type', 'param_type',
    'ident', 'member_accesses', 'find', 'params', 'attrs',
    // C# — listing modes (query_single_file only)
    'methods', 'fields', 'classes', 'usings',
    // Python — pattern modes
    'decorators',
    // Python — listing modes (query_single_file only)
    'imports',
] as const;
export type QueryMode = typeof QUERY_MODES[number];

export function queryAst(
    config: CodesearchConfig,
    mode: string,
    pattern: string,
    files: string[],
): Promise<QueryFileResult[]> {
    const port   = (config.port ?? 8108) + 1;
    const apiKey = config.api_key ?? 'codesearch-local';
    const body   = JSON.stringify({ mode, pattern, files });

    return new Promise((resolve, reject) => {
        const req = http.request(
            {
                hostname: 'localhost',
                port,
                path: '/query',
                method: 'POST',
                headers: {
                    'X-TYPESENSE-API-KEY': apiKey,
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(body),
                },
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    try {
                        const parsed = JSON.parse(data);
                        if (res.statusCode && res.statusCode >= 400) {
                            reject(new Error(`Query API ${res.statusCode}: ${(parsed as { error?: string }).error ?? data.slice(0, 200)}`));
                        } else {
                            resolve((parsed as { results: QueryFileResult[] }).results ?? []);
                        }
                    } catch {
                        reject(new Error(`Bad JSON from query API: ${data.slice(0, 200)}`));
                    }
                });
            }
        );
        req.setTimeout(15000, () => req.destroy(new Error('Query API timed out')));
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

// ---------------------------------------------------------------------------
// Full search pipeline: unified /query-codebase endpoint
// ---------------------------------------------------------------------------

export interface PipelineHit {
    document: {
        id?: string;
        relative_path: string;
        subsystem?: string;
        filename?: string;
    };
    _matches: MatchItem[];
}

export interface PipelineResult {
    hits: PipelineHit[];
    found: number;           // Typesense count (before AST filter)
    tsFound: number;         // Same as found (kept for backwards compatibility)
    elapsed: number;
    facet_counts: TypesenseResult['facet_counts'];
    overflow?: boolean;
}

/**
 * Call the indexserver's /query-codebase endpoint, which performs
 * Typesense pre-filter + AST post-filter in one call on the server.
 */
export async function doQueryCodebase(
    config: CodesearchConfig,
    query: string,
    mode: string,
    ext: string,
    sub: string,
    rootName: string,
    limit: number,
): Promise<{ found: number; overflow: boolean; hits: PipelineHit[]; facet_counts: TypesenseResult['facet_counts'] }> {
    const port   = (config.port ?? 8108) + 1;
    const apiKey = config.api_key ?? 'codesearch-local';
    const body   = JSON.stringify({ mode, pattern: query, sub, ext, root: rootName, limit });

    return new Promise((resolve, reject) => {
        const req = http.request(
            {
                hostname: 'localhost',
                port,
                path: '/query-codebase',
                method: 'POST',
                headers: {
                    'X-TYPESENSE-API-KEY': apiKey,
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(body),
                },
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    try {
                        const parsed = JSON.parse(data) as {
                            found: number; overflow: boolean;
                            hits: Array<{ document: { id: string; relative_path: string; subsystem: string; filename: string }; matches: Array<{ line: number; text: string }> }>;
                            facet_counts: TypesenseResult['facet_counts'];
                            error?: string;
                        };
                        if (res.statusCode && res.statusCode >= 400) {
                            reject(new Error(`Query-codebase API ${res.statusCode}: ${parsed.error ?? data.slice(0, 200)}`));
                        } else {
                            const hits: PipelineHit[] = (parsed.hits ?? []).map((h) => ({
                                document: {
                                    id:            h.document.id,
                                    relative_path: h.document.relative_path,
                                    subsystem:     h.document.subsystem,
                                    filename:      h.document.filename,
                                },
                                _matches: (h.matches ?? []).map((m) => ({
                                    text: m.text,
                                    line: m.line - 1,  // 1-indexed → 0-indexed
                                })),
                            }));
                            resolve({
                                found:        parsed.found ?? 0,
                                overflow:     parsed.overflow ?? false,
                                hits,
                                facet_counts: parsed.facet_counts,
                            });
                        }
                    } catch {
                        reject(new Error(`Bad JSON from query-codebase API: ${data.slice(0, 200)}`));
                    }
                });
            }
        );
        req.setTimeout(30000, () => req.destroy(new Error('Query-codebase API timed out')));
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

/**
 * Run a full search using the server's /query-codebase endpoint.
 * The server handles Typesense pre-filter + AST post-filter in one call.
 */
export async function runSearchPipeline(
    config: CodesearchConfig,
    query: string,
    modeKey: string,
    ext: string,
    sub: string,
    rootName: string,
    limit: number,
    rootPath?: string,  // kept for backwards compatibility, no longer used
): Promise<PipelineResult> {
    const start = Date.now();
    const result = await doQueryCodebase(config, query, modeKey, ext, sub, rootName, limit);
    return {
        hits:         result.hits,
        found:        result.found,
        tsFound:      result.found,
        elapsed:      Date.now() - start,
        facet_counts: result.facet_counts,
        overflow:     result.overflow,
    };
}

/**
 * Render a pipeline result as a text tree, mirroring the webview layout.
 * Useful for validating output in tests and CLI tools.
 */
export function renderTextTree(result: PipelineResult, query: string, mode: string): string {
    const lines: string[] = [];
    const modeEntry = MODES.find((m) => m.key === mode);
    const modeLabel = modeEntry?.label ?? mode;
    const isAst = !!modeEntry?.astMode;

    lines.push(`Query: "${query}"  Mode: ${modeLabel}  Found: ${result.found}` +
        (isAst ? ` (Typesense: ${result.tsFound})` : '') +
        `  Elapsed: ${result.elapsed}ms`);

    if (result.found === 0) { lines.push('  (no results)'); return lines.join('\n'); }

    // Group by subsystem
    const bySub = new Map<string, PipelineHit[]>();
    for (const h of result.hits) {
        const s = h.document.subsystem ?? '';
        if (!bySub.has(s)) { bySub.set(s, []); }
        bySub.get(s)!.push(h);
    }

    for (const [sub, subHits] of [...bySub.entries()].sort()) {
        lines.push(`\n[${sub || '(no subsystem)'}]  ${subHits.length} file(s)`);
        for (const h of subHits) {
            lines.push(`  ${h.document.relative_path}`);
            const matches = h._matches;
            for (let i = 0; i < Math.min(matches.length, 10); i++) {
                const m = matches[i];
                const branch = i === matches.length - 1 || i === 9 ? '└─' : '├─';
                const lineNum = m.line !== undefined ? `:${m.line + 1}` : '';
                lines.push(`    ${branch} ${lineNum.padEnd(6)} ${m.text.trim().slice(0, 120)}`);
            }
            if (matches.length > 10) {
                lines.push(`    └─ … ${matches.length - 10} more matches`);
            }
        }
    }
    return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Caller-site enrichment (legacy regex fallback — use queryCs for accuracy)
// ---------------------------------------------------------------------------

export interface CallerSite {
    line: number;   // 0-indexed
    text: string;   // trimmed line content
}

export function enrichCallersHits(
    hits: TypesenseHit[],
    query: string,
    rootPath: string,
): Array<TypesenseHit & { _callerSites: CallerSite[] }> {
    const methodName = (query.includes('.') ? query.split('.').pop()! : query).trim();
    const escaped = methodName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = escaped ? new RegExp(`\\b${escaped}\\s*[<(]`, 'i') : null;

    return hits.map((hit) => {
        const enriched = hit as TypesenseHit & { _callerSites: CallerSite[] };
        if (!pattern) { enriched._callerSites = []; return enriched; }
        try {
            const fullPath = resolveFilePath(rootPath, hit.document.relative_path);
            const lines = fs.readFileSync(fullPath, 'utf-8').split('\n');
            enriched._callerSites = [];
            for (let i = 0; i < lines.length; i++) {
                if (pattern.test(lines[i])) {
                    enriched._callerSites.push({ line: i, text: lines[i].trim() });
                }
            }
        } catch {
            enriched._callerSites = [];
        }
        return enriched;
    });
}

// ---------------------------------------------------------------------------
// Path helpers — returns string so vscode.Uri wrapping stays in extension.ts
// ---------------------------------------------------------------------------

export function resolveFilePath(rootPath: string, relativePath: string): string {
    const root = rootPath.replace(/\\/g, '/').replace(/\/$/, '');
    const rel = relativePath.replace(/\\/g, '/').replace(/^\//, '');
    // WSL path on Windows: /mnt/c/foo -> C:/foo
    const wslMatch = root.match(/^\/mnt\/([a-z])\/(.*)/i);
    const winRoot = wslMatch ? `${wslMatch[1].toUpperCase()}:/${wslMatch[2]}` : root;
    return `${winRoot}/${rel}`;
}
