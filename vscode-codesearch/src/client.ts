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
        method_sigs?: string[];
        type_refs?: string[];
        attributes?: string[];
        usings?: string[];
    };
    highlights?: Array<{
        field: string;
        snippet?: string;
        snippets?: string[];
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

export const MODES: Array<{ key: string; label: string; queryBy: string; weights: string; desc: string }> = [
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
        desc: 'Search only C# class/interface/method/property names',
    },
    {
        key: 'implements',
        label: 'Implements',
        queryBy: 'base_types,class_names,filename',
        weights: '4,3,2',
        desc: 'Find types that implement or inherit from the given interface/class [T1]',
    },
    {
        key: 'callers',
        label: 'Callers',
        queryBy: 'call_sites,filename',
        weights: '4,2',
        desc: 'Find files that call the given method [T1]',
    },
    {
        key: 'sig',
        label: 'Signature',
        queryBy: 'method_sigs,method_names,filename',
        weights: '4,3,2',
        desc: 'Search method signatures (return type, parameter types) [T1]',
    },
    {
        key: 'uses',
        label: 'Type Refs',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Find files that reference the given type in declarations [T2]',
    },
    {
        key: 'attr',
        label: 'Attributes',
        queryBy: 'attributes,filename',
        weights: '4,2',
        desc: 'Find files decorated with the given attribute [T2]',
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

/**
 * Valid tree-sitter query modes for queryCs — mirrors the VALID_MODES list
 * in mcp_server.py's query_ast tool.
 */
export const QUERY_MODES = [
    'classes', 'methods', 'fields', 'calls', 'implements', 'uses',
    'field_type', 'param_type', 'casts', 'ident', 'member_accesses',
    'attrs', 'usings', 'find', 'params',
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
