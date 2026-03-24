/**
 * Pure business logic for the codesearch extension.
 * No vscode imports — safe to test in plain Node.js.
 */
import * as fs from 'fs';
import * as http from 'http';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RootEntry {
    windows_path: string;
    local_path?: string;
}

export interface CodesearchConfig {
    api_key: string;
    port: number;
    mode?: 'docker' | 'wsl';
    roots: Record<string, RootEntry>;
    docker_container?: string;
    docker_image?: string;
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
    astMode?: string; uses_kind?: string;
}> = [
    {
        key: 'text',
        label: 'Text',
        queryBy: 'filename,symbols,class_names,method_names,content',
        weights: '5,4,4,4,1',
        desc: 'Full-text search across filenames, symbols, and file content',
    },
    {
        key: 'declarations',
        label: 'Declarations',
        queryBy: 'method_sigs,method_names,symbols,filename',
        weights: '4,3,3,2',
        desc: 'Find declaration/signature of the named method/type/property [method_sigs]',
        astMode: 'declarations',
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
        key: 'all_refs',
        label: 'All Refs',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Every identifier occurrence (semantic grep, skips comments/strings) [type_refs]',
        astMode: 'all_refs',
    },
    {
        key: 'accesses_on',
        label: 'Accesses On',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: '.Member accesses on locals/params of the given type [type_refs]',
        astMode: 'accesses_on',
    },
    {
        key: 'uses_field',
        label: 'Uses (Field)',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Fields/properties declared with the given type [type_refs]',
        astMode: 'uses',
        uses_kind: 'field',
    },
    {
        key: 'uses_param',
        label: 'Uses (Param)',
        queryBy: 'type_refs,symbols,class_names,filename',
        weights: '4,3,3,2',
        desc: 'Method/constructor parameters typed as the given type [type_refs]',
        astMode: 'uses',
        uses_kind: 'param',
    },
];

// ---------------------------------------------------------------------------
// Config helpers
// ---------------------------------------------------------------------------

export function loadConfig(configPath: string): CodesearchConfig {
    return JSON.parse(fs.readFileSync(configPath, 'utf-8'));
}

export function getRoots(config: CodesearchConfig): Record<string, string> {
    return Object.fromEntries(
        Object.entries(config.roots).map(([k, v]) => [k, v.windows_path])
    );
}

export function sanitizeName(name: string): string {
    return name.toLowerCase().replace(/[^a-z0-9_]/g, '_');
}

export function collectionForRoot(name: string): string {
    return `codesearch_${sanitizeName(name)}`;
}

// ---------------------------------------------------------------------------
// Full search pipeline: unified /query-codebase endpoint
// ---------------------------------------------------------------------------

/** A single match item displayed under a file node in the results tree. */
export interface MatchItem { text: string; line?: number; }  // line is 0-indexed

export interface PipelineHit {
    document: {
        id?: string;
        relative_path: string;
        subsystem?: string;
        filename?: string;
    };
    _matches: MatchItem[];
    /** True (default) = AST was run and these are exact match lines.
     *  False = Typesense matched the file but AST was not run (too many results). */
    ast_expanded?: boolean;
}

export type FacetCounts = Array<{
    field_name: string;
    counts: Array<{ value: string; count: number }>;
}>;

export interface PipelineResult {
    hits: PipelineHit[];
    found: number;
    elapsed: number;
    facet_counts: FacetCounts | undefined;
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
): Promise<{ found: number; overflow: boolean; hits: PipelineHit[]; facet_counts: FacetCounts | undefined }> {
    const modeEntry = MODES.find((m) => m.key === mode);
    const serverMode = modeEntry?.astMode ?? mode;
    const port   = config.port + 1;
    const apiKey = config.api_key;
    const bodyObj: Record<string, unknown> = { mode: serverMode, pattern: query, sub, ext, root: rootName, limit };
    if (modeEntry?.uses_kind) { bodyObj['uses_kind'] = modeEntry.uses_kind; }
    const body   = JSON.stringify(bodyObj);

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
                            hits: Array<{ document: { id: string; relative_path: string; subsystem: string; filename: string }; matches: Array<{ line: number; text: string }>; ast_expanded?: boolean }>;
                            facet_counts: FacetCounts | undefined;
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
                                ast_expanded: h.ast_expanded !== false,  // default true for old responses
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
 * Run a tree-sitter AST query on a single file via the /query endpoint.
 * absolutePath must be a Windows path (e.g. q:/spocore/src/foo.cs).
 */
export async function doQuerySingleFile(
    config: CodesearchConfig,
    modeKey: string,
    pattern: string,
    absolutePath: string,
): Promise<MatchItem[]> {
    const modeEntry  = MODES.find((m) => m.key === modeKey);
    const serverMode = modeEntry?.astMode ?? modeKey;
    const port   = config.port + 1;
    const apiKey = config.api_key;
    const bodyObj: Record<string, unknown> = { mode: serverMode, pattern, files: [absolutePath] };
    if (modeEntry?.uses_kind) { bodyObj['uses_kind'] = modeEntry.uses_kind; }
    const body = JSON.stringify(bodyObj);

    return new Promise((resolve, reject) => {
        const req = http.request(
            {
                hostname: 'localhost', port, path: '/query', method: 'POST',
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
                            results?: Array<{ matches?: Array<{ line: number; text: string }> }>;
                            error?: string;
                        };
                        if (res.statusCode && res.statusCode >= 400) {
                            reject(new Error(`Query API ${res.statusCode}: ${parsed.error ?? data.slice(0, 200)}`));
                        } else {
                            const fileResult = (parsed.results ?? [])[0];
                            const matches: MatchItem[] = (fileResult?.matches ?? []).map((m) => ({
                                text: m.text,
                                line: m.line - 1,  // 1-indexed → 0-indexed
                            }));
                            resolve(matches);
                        }
                    } catch {
                        reject(new Error(`Bad JSON from query API: ${data.slice(0, 200)}`));
                    }
                });
            },
        );
        req.setTimeout(10000, () => req.destroy(new Error('Query API timed out')));
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
): Promise<PipelineResult> {
    const start = Date.now();
    const result = await doQueryCodebase(config, query, modeKey, ext, sub, rootName, limit);
    return {
        hits:         result.hits,
        found:        result.found,
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

    lines.push(`Query: "${query}"  Mode: ${modeLabel}  Found: ${result.found}  Elapsed: ${result.elapsed}ms`);

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
