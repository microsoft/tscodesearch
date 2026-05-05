/**
 * HTTP helper class for the codesearch management API.
 *
 * The Windows filesystem watcher has been moved to tsquery_server.py (the
 * management server daemon), which uses watchdog's ReadDirectoryChangesW
 * observer directly.  This class now only provides the apiPost/apiGet
 * helpers used by StatusBarManager and the reindex command.
 */

import * as http from 'http';
import { CodesearchConfig } from './client';

export class FileWatcher {
    readonly apiPort: number;
    readonly apiKey:  string;

    /** Always false — file watching is now handled by the daemon. */
    get isActive(): boolean { return false; }

    constructor(config: CodesearchConfig, _out: unknown) {
        this.apiPort = (config.port ?? 8108) + 1;
        this.apiKey  = config.api_key;
    }

    apiPost(path: string, body?: unknown): Promise<Record<string, unknown> | null> {
        return new Promise((resolve) => {
            const bodyStr = body ? JSON.stringify(body) : '';
            const req = http.request(
                {
                    hostname: 'localhost',
                    port:     this.apiPort,
                    path,
                    method:   'POST',
                    headers:  {
                        'X-TYPESENSE-API-KEY': this.apiKey,
                        ...(body ? {
                            'Content-Type':   'application/json',
                            'Content-Length': Buffer.byteLength(bodyStr),
                        } : {}),
                    },
                },
                (res) => {
                    let data = '';
                    res.on('data', (chunk: Buffer) => { data += chunk; });
                    res.on('end', () => {
                        try { resolve(JSON.parse(data) as Record<string, unknown>); }
                        catch { resolve(null); }
                    });
                },
            );
            req.setTimeout(5000, () => { req.destroy(); resolve(null); });
            req.on('error', () => resolve(null));
            if (body) { req.write(bodyStr); }
            req.end();
        });
    }

    apiGet(path: string): Promise<Record<string, unknown> | null> {
        return new Promise((resolve) => {
            const req = http.request(
                {
                    hostname: 'localhost',
                    port:     this.apiPort,
                    path,
                    method:   'GET',
                    headers:  { 'X-TYPESENSE-API-KEY': this.apiKey },
                },
                (res) => {
                    let data = '';
                    res.on('data', (chunk: Buffer) => { data += chunk; });
                    res.on('end', () => {
                        try { resolve(JSON.parse(data) as Record<string, unknown>); }
                        catch { resolve(null); }
                    });
                },
            );
            req.setTimeout(5000, () => { req.destroy(); resolve(null); });
            req.on('error', () => resolve(null));
            req.end();
        });
    }

    dispose(): void { /* nothing to clean up */ }
}
