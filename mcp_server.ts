#!/usr/bin/env node
/**
 * Node.js MCP server for tscodesearch.
 *
 * Runs on Windows (stdio transport) — no WSL required.
 * The Python indexserver + Typesense run in the Docker container.
 *
 * Tools:
 *   query_codebase     - Typesense pre-filter + tree-sitter AST (via indexserver /query-codebase)
 *   query_single_file  - Tree-sitter AST on one file (via indexserver /query)
 *   ready              - Quick index health snapshot
 *   verify_index       - Start/stop/monitor index repair scan
 *   service_status     - Typesense + indexserver status
 *   manage_service     - Docker container lifecycle (start/stop/restart/rebuild)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import * as fs from "fs";
import * as http from "http";
import * as path from "path";
import { spawnSync } from "child_process";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Config ────────────────────────────────────────────────────────────────────

interface RootEntry {
  windows_path: string;
  local_path?: string;
}

interface Config {
  api_key: string;
  port: number;
  roots: Record<string, RootEntry>;
  docker_container: string;
}

function loadConfig(): Config {
  const configPath = path.join(__dirname, "config.json");
  let raw: any;
  try {
    raw = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  } catch (e: any) {
    throw new Error(`Cannot read config.json at ${configPath}: ${e.message}`);
  }
  if (!raw.port) throw new Error(`'port' is required in ${configPath}`);
  return {
    api_key:          raw.api_key ?? "codesearch-local",
    port:             Number(raw.port),
    roots:            raw.roots ?? {},
    docker_container: raw.docker_container ?? "codesearch",
  };
}

const cfg = loadConfig();
const API_PORT        = cfg.port + 1;
const API_KEY         = cfg.api_key;
const ROOTS           = cfg.roots;
const DOCKER_CONTAINER = cfg.docker_container;

// ── HTTP helpers ──────────────────────────────────────────────────────────────

interface HttpResult { status: number; data: any }

function httpGet(hostname: string, port: number, urlPath: string, extraHeaders?: Record<string, string>, timeoutMs = 10_000): Promise<HttpResult> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { hostname, port, path: urlPath, method: "GET", headers: extraHeaders, timeout: timeoutMs },
      (res) => {
        let raw = "";
        res.on("data", (c) => { raw += c; });
        res.on("end", () => {
          try { resolve({ status: res.statusCode ?? 0, data: JSON.parse(raw) }); }
          catch { resolve({ status: res.statusCode ?? 0, data: raw }); }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error(`GET ${urlPath} timed out`)));
    req.end();
  });
}

function httpPost(port: number, urlPath: string, body: object, timeoutMs = 120_000): Promise<HttpResult> {
  const bodyStr = JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "localhost", port, path: urlPath, method: "POST", timeout: timeoutMs,
        headers: {
          "X-TYPESENSE-API-KEY": API_KEY,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(bodyStr),
        },
      },
      (res) => {
        let raw = "";
        res.on("data", (c) => { raw += c; });
        res.on("end", () => {
          try { resolve({ status: res.statusCode ?? 0, data: JSON.parse(raw) }); }
          catch { resolve({ status: res.statusCode ?? 0, data: raw }); }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error(`POST ${urlPath} timed out`)));
    req.write(bodyStr);
    req.end();
  });
}

function apiGet(urlPath: string, timeoutMs = 10_000): Promise<HttpResult> {
  return httpGet("localhost", API_PORT, urlPath, { "X-TYPESENSE-API-KEY": API_KEY }, timeoutMs);
}

// ── Config helpers ────────────────────────────────────────────────────────────

function collectionForRoot(name: string): string {
  return `codesearch_${name.toLowerCase().replace(/[^a-z0-9_]/g, "_")}`;
}

function getRoot(name: string): [string, string] {
  const effective = name || ("default" in ROOTS ? "default" : Object.keys(ROOTS)[0] ?? "");
  if (!effective || !(effective in ROOTS)) {
    throw new Error(`Unknown root '${name}'. Available: ${Object.keys(ROOTS).sort().join(", ")}`);
  }
  return [collectionForRoot(effective), ROOTS[effective].windows_path];
}

/**
 * Convert a file path to the container-internal /source/<name>/… path.
 *
 * Accepts:
 *   - Full Windows host path  C:/repos/src/Foo.cs  → /source/default/Foo.cs
 *   - /mnt/c/… WSL path       → normalised to C:/… then matched as above
 *   - $SRC_ROOT/… substitution
 *   - Bare relative path      Foo.cs or sub/Foo.cs  → prepends the default
 *     host root (from config.json roots) before matching, so callers can pass
 *     a path as returned by query_single_file without needing to qualify it
 */
function toContainerPath(filePath: string): string {
  let p = filePath.replace(/\\/g, "/");

  // Expand $SRC_ROOT
  const defaultRootEntry = ROOTS["default"] ?? Object.values(ROOTS)[0];
  const defaultRoot = defaultRootEntry?.windows_path ?? "";
  p = p.replace(/\$\{SRC_ROOT\}/g, defaultRoot).replace(/\$SRC_ROOT/g, defaultRoot);

  // /mnt/c/… → C:/…
  const mntM = p.match(/^\/mnt\/([a-z])\/(.*)/i);
  if (mntM) p = `${mntM[1].toUpperCase()}:/${mntM[2]}`;

  const pLower = p.toLowerCase();
  for (const [name, entry] of Object.entries(ROOTS)) {
    const root = entry.windows_path.replace(/\\/g, "/").replace(/\/$/, "");
    if (pLower.startsWith(root.toLowerCase() + "/") || pLower === root.toLowerCase()) {
      const rel = p.slice(root.length); // includes leading /
      return `/source/${name}${rel}`;
    }
  }

  // Bare relative path (no drive letter, not already a container path) —
  // prefix with the default host root and retry.
  if (!p.match(/^[A-Za-z]:/) && !p.startsWith("/source/")) {
    const hostRoot = defaultRoot.replace(/\/$/, "");
    if (hostRoot) return toContainerPath(hostRoot + "/" + p);
  }

  // Already a container path or unknown — pass through
  return p;
}

// ── Queue warning ─────────────────────────────────────────────────────────────

async function queueWarning(): Promise<string> {
  try {
    const { status, data } = await apiGet("/status", 2_000);
    if (status !== 200) return "";
    const depth          = data?.queue?.depth ?? 0;
    const indexerRunning = data?.indexer?.running ?? false;
    const parts: string[] = [];
    if (depth > 0) parts.push(`${depth} files queued`);
    if (indexerRunning) parts.push("indexer walk in progress");
    if (parts.length) return `[WARNING: index has outstanding work — ${parts.join(", ")}. Results may be incomplete.]\n\n`;
  } catch { /* indexserver not running */ }
  return "";
}

// ── MCP server ────────────────────────────────────────────────────────────────

const server = new McpServer({ name: "tscodesearch", version: "1.0.0" });

const MAX_OUTPUT_CHARS     = 40_000;
const QUERY_CODEBASE_LIMIT = 250;

// ── query_codebase ────────────────────────────────────────────────────────────

server.tool(
  "query_codebase",
  `Typesense pre-filter + tree-sitter AST in one call. Returns exact line-level results.
NEVER returns partial results. If the search matches more than 250 files, returns a
per-subsystem breakdown — repeat with sub= to narrow.

For listing modes (methods, fields, classes, usings, imports) use query_single_file.

Args:
  mode:         text, declarations, calls, implements, uses, casts, attrs,
                accesses_of, accesses_on, all_refs (C#);
                calls, implements, ident, declarations, params, decorators (Python)
  pattern:      Type/method/name to search for.
  sub:          Narrow to a subsystem (first path component only).
  ext:          File extension ("cs" or "py"). Default: cs.
  context_lines: Surrounding source lines per match.
  root:         Named source root (empty = default).
  include_body: For declarations — include full body. Default false.
  symbol_kind:  For declarations — restrict to: method, class, interface, etc.
  uses_kind:    For uses — all, field, param, return, cast, base, locals.

Examples:
  query_codebase("calls", "SaveChanges", sub="services")
  query_codebase("uses", "IDataStore", uses_kind="param", sub="services")
  query_codebase("implements", "IRepository")
  query_codebase("declarations", "SaveChanges", symbol_kind="method")`,
  {
    mode:          z.string(),
    pattern:       z.string(),
    sub:           z.string().default(""),
    ext:           z.string().default(""),
    context_lines: z.number().int().default(0),
    root:          z.string().default(""),
    include_body:  z.boolean().default(false),
    symbol_kind:   z.string().default(""),
    uses_kind:     z.string().default(""),
  },
  async ({ mode, pattern, sub, ext, context_lines, root, include_body, symbol_kind, uses_kind }) => {
    const LISTING = new Set(["methods","fields","classes","usings","imports"]);
    const m = mode.toLowerCase().trim().replace(/-/g, "_");
    if (LISTING.has(m)) {
      return { content: [{ type: "text" as const, text: `Mode '${m}' lists file contents without filtering — use query_single_file instead:\n  query_single_file("${m}", file="$SRC_ROOT/path/to/File.cs")` }] };
    }

    let result: HttpResult;
    try {
      result = await httpPost(API_PORT, "/query-codebase", {
        mode: m, pattern, sub: sub || "", ext: (ext || "cs").replace(/^\./, ""),
        root: root || "", limit: QUERY_CODEBASE_LIMIT,
        include_body, symbol_kind: symbol_kind || "", uses_kind: uses_kind || "",
      });
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Could not reach indexserver: ${e.message}\nStart it with: ts start` }] };
    }

    const warn = await queueWarning();

    if (result.status >= 400) {
      const err    = result.data?.error   ?? JSON.stringify(result.data);
      const detail = result.data?.detail  ?? "";
      let msg = `TSCODESEARCH ERROR — do not fall back to Grep/Glob; investigate and fix.\nError from indexserver: ${err}`;
      if (detail) msg += `\nDetail: ${detail}`;
      return { content: [{ type: "text" as const, text: warn + msg }] };
    }

    const data   = result.data;
    const found: number  = data.found ?? 0;
    const hits: any[]    = data.hits  ?? [];
    const facets: any[]  = data.facet_counts ?? [];

    if (data.overflow) {
      const lines = [`Too many files (${found}) — narrowing required.`, "Repeat with sub= to scope to one subsystem, then re-run.", ""];
      if (!sub) {
        const counts: Array<[string, number]> = [];
        for (const fc of facets) if (fc.field_name === "subsystem") for (const c of fc.counts ?? []) counts.push([c.value, Number(c.count)]);
        if (counts.length) {
          counts.sort((a, b) => b[1] - a[1]);
          lines.push(`Subsystems with '${pattern}' hits — re-run with sub=<name>:`);
          for (const [name, count] of counts.slice(0, 25)) lines.push(`  query_codebase("${m}", "${pattern}", sub="${name}")  # ~${count} files`);
        }
      }
      lines.push("", "Use query_single_file for a specific known file.");
      return { content: [{ type: "text" as const, text: warn + lines.join("\n") }] };
    }

    const header = `[Typesense: ${found} files | AST scanned: ${found} | files with matches: ${hits.length}]\n`;
    if (!hits.length) return { content: [{ type: "text" as const, text: warn + header + "No AST matches found." }] };

    const outLines: string[] = [];
    for (const hit of hits) {
      const rel = hit.document?.relative_path ?? "";
      for (const match of hit.matches ?? []) outLines.push(`${rel}:${match.line}: ${(match.text ?? "").trimEnd()}`);
    }
    const output = outLines.join("\n");
    if (!output) return { content: [{ type: "text" as const, text: warn + header + "No AST matches found." }] };

    if (output.length > MAX_OUTPUT_CHARS) {
      const trunc = output.slice(0, MAX_OUTPUT_CHARS);
      const nl    = trunc.lastIndexOf("\n");
      const shown = (nl > 0 ? trunc.slice(0, nl) : trunc).split("\n").length;
      const summary = `[Result truncated — ${outLines.length} matches. Showing first ${shown} lines.]\n\n`;
      return { content: [{ type: "text" as const, text: warn + header + summary + (nl > 0 ? trunc.slice(0, nl) : trunc) }] };
    }
    return { content: [{ type: "text" as const, text: warn + header + output }] };
  }
);

// ── query_single_file ─────────────────────────────────────────────────────────

server.tool(
  "query_single_file",
  `Run a tree-sitter AST query on a single file. No Typesense search.

Supports all modes including listing modes (methods, fields, classes, usings, imports).
Works well on large source files — tree-sitter parses the whole file and returns only matching nodes.

Args:
  mode:    AST query mode.
           C# pattern-required: uses, calls, implements, casts, declarations,
             attrs, accesses_of, accesses_on, all_refs, params
           C# listing (no pattern): methods, fields, classes, usings
           Python pattern-required: calls, implements, ident, declarations, decorators, params
           Python listing (no pattern): classes, methods, imports
  pattern: Type/method/name to search for. Omit for listing modes.
  file:    Absolute path to the file. Accepts Windows paths (C:/…), /mnt/c/… paths,
           or $SRC_ROOT-prefixed paths. Relative paths are NOT supported.
  context_lines: Surrounding source lines per match.
  root:    Named source root (empty = default).
  include_body: For declarations — include full body. Default false.
  symbol_kind:  For declarations — restrict to a specific kind.
  uses_kind:    For uses — all, field, param, return, cast, base, locals.

Examples:
  query_single_file("methods", file="$SRC_ROOT/services/Widget.cs")
  query_single_file("calls", "SaveChanges", file="$SRC_ROOT/data/Widget.cs")
  query_single_file("uses", "IRepository", uses_kind="param", file="$SRC_ROOT/services/Widget.cs")
  query_single_file("accesses_on", "IDataStore", file="$SRC_ROOT/services/DataManager.cs")`,
  {
    mode:          z.string(),
    pattern:       z.string().default(""),
    file:          z.string().default(""),
    context_lines: z.number().int().default(0),
    root:          z.string().default(""),
    include_body:  z.boolean().default(false),
    symbol_kind:   z.string().default(""),
    uses_kind:     z.string().default(""),
  },
  async ({ mode, pattern, file, context_lines, root, include_body, symbol_kind, uses_kind }) => {
    if (!file) return { content: [{ type: "text" as const, text: "file= is required." }] };

    let srcRoot: string;
    try { [, srcRoot] = getRoot(root); }
    catch (e: any) { return { content: [{ type: "text" as const, text: `Error: ${e.message}` }] }; }

    const m             = mode.toLowerCase().trim().replace(/-/g, "_");
    const containerPath = toContainerPath(file);

    let result: HttpResult;
    try {
      result = await httpPost(API_PORT, "/query", {
        mode: m, pattern: pattern || "", files: [containerPath],
        include_body, symbol_kind: symbol_kind || "", uses_kind: uses_kind || "",
      });
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Could not reach indexserver: ${e.message}\nStart it with: ts start` }] };
    }

    if (result.status >= 400) {
      return { content: [{ type: "text" as const, text: `Query failed (${result.status}): ${result.data?.error ?? JSON.stringify(result.data)}` }] };
    }

    // Build relative path for display header
    const normFile = file.replace(/\\/g, "/")
      .replace(/\$\{?SRC_ROOT\}?/g, srcRoot.replace(/\\/g, "/"));
    const normRoot  = srcRoot.replace(/\\/g, "/").replace(/\/$/, "");
    const normLower = normFile.toLowerCase();
    const rel = normLower.startsWith(normRoot.toLowerCase() + "/")
      ? normFile.slice(normRoot.length + 1)
      : normFile;
    const header = `[${rel}]\n`;

    const fileResult = (result.data?.results ?? [])[0];
    if (!fileResult?.matches?.length) return { content: [{ type: "text" as const, text: header + "No matches found." }] };

    const outLines: string[] = fileResult.matches.map((m: any) => `${rel}:${m.line}: ${(m.text ?? "").trimEnd()}`);
    const output = outLines.join("\n");

    if (output.length > MAX_OUTPUT_CHARS) {
      const trunc = output.slice(0, MAX_OUTPUT_CHARS);
      const nl    = trunc.lastIndexOf("\n");
      const shown = (nl > 0 ? trunc.slice(0, nl) : trunc).split("\n").length;
      const summary = `[Result truncated — ${outLines.length} lines. Showing first ${shown} lines.]\n\n`;
      return { content: [{ type: "text" as const, text: header + summary + (nl > 0 ? trunc.slice(0, nl) : trunc) }] };
    }
    return { content: [{ type: "text" as const, text: header + output }] };
  }
);

// ── ready ─────────────────────────────────────────────────────────────────────

server.tool(
  "ready",
  `Check whether the code search index is fully up to date with the file system.

Returns a quick status snapshot (no filesystem walk — returns immediately).
Shows Typesense health, document count, watcher state, queue depth, and last verifier scan.

To trigger a full repair scan use verify_index(action='start'), then poll
ready() or verify_index(action='status') until complete.

Args:
  root: Named source root to check (empty = default root).`,
  { root: z.string().default("") },
  async ({ root }) => {
    let collection: string;
    try { [collection] = getRoot(root); }
    catch (e: any) { return { content: [{ type: "text" as const, text: `Error: ${e.message}` }] }; }

    let st: any;
    try {
      const { status, data } = await apiGet("/status");
      if (status !== 200) throw new Error(`HTTP ${status}`);
      st = data;
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Indexserver is NOT running: ${e.message}\nStart it with: ts start` }] };
    }

    const rootName  = root || ("default" in ROOTS ? "default" : Object.keys(ROOTS)[0] ?? "");
    const colInfo   = st.collections?.[rootName] ?? {};
    const ndocs     = colInfo.num_documents;
    const lines: string[] = [];

    lines.push(`Typesense  : ${st.typesense_ok !== false ? "ok" : "NOT OK"}`);
    if (ndocs != null) {
      lines.push(`Docs       : ${Number(ndocs).toLocaleString()}  (collection: ${collection})`);
    } else {
      lines.push(`Collection : ${collection} — not found`);
    }

    const watcher  = st.watcher  ?? {};
    const queue    = st.queue    ?? {};
    const verifier = st.verifier ?? {};
    const wState   = watcher.running ? "running" : (watcher.paused ? "paused" : "stopped");
    lines.push(`Watcher    : ${wState}`);
    lines.push(`Queue      : ${queue.depth ?? 0} pending  (${queue.total_queued ?? 0} total processed)`);

    const vp = verifier.progress ?? {};
    if (Object.keys(vp).length) {
      const vstatus  = vp.status ?? "?";
      const missing  = vp.missing  ?? 0;
      const stale    = vp.stale    ?? 0;
      const orphaned = vp.orphaned ?? 0;
      const total    = vp.total_to_update ?? 0;
      const updated  = vp.updated ?? 0;
      const remaining = Math.max(0, total - updated) || (missing + stale);
      lines.push(`Verifier   : ${vstatus}  phase=${vp.phase ?? ""}  missing=${missing}  stale=${stale}  orphaned=${orphaned}  updated=${updated}/${total}  (last: ${vp.last_update ?? "?"})`);
      const qDepth = queue.depth ?? 0;
      const left   = remaining + qDepth;
      if (vstatus === "complete" && missing === 0 && stale === 0 && orphaned === 0 && qDepth === 0) {
        lines.push("Left to index: 0  — index is up to date");
      } else if (vstatus === "running") {
        lines.push(`Left to index: ~${left}  (${remaining} verifier + ${qDepth} queue) — poll again for updates`);
      } else {
        lines.push("Left to index: unknown — run verify_index(action='start') to check and repair");
      }
    } else {
      lines.push("Verifier   : no scan has been run yet");
      lines.push(`Left to index: ${queue.depth ?? 0} queued — run verify_index(action='start') to check if index is complete`);
    }

    return { content: [{ type: "text" as const, text: lines.join("\n") }] };
  }
);

// ── verify_index ──────────────────────────────────────────────────────────────

server.tool(
  "verify_index",
  `Verify that the code search index is up to date with the file system.

Scans every source file, compares modification times against stored values,
and re-indexes missing or stale files. Orphaned entries are removed unless
delete_orphans=false.

Args:
  action:         "start" | "status" | "stop"
  root:           Named source root to verify (empty = default root).
  delete_orphans: Remove entries for deleted files. Default true.`,
  {
    action:         z.string().default("status"),
    root:           z.string().default(""),
    delete_orphans: z.boolean().default(true),
  },
  async ({ action, root, delete_orphans }) => {
    const act = action.toLowerCase().trim();

    if (act === "stop") {
      const { status, data } = await httpPost(API_PORT, "/verify/stop", {});
      if (status === 404) return { content: [{ type: "text" as const, text: "No verification scan is currently running." }] };
      if (status !== 200) return { content: [{ type: "text" as const, text: `Stop failed (${status}): ${data?.error ?? JSON.stringify(data)}` }] };
      return { content: [{ type: "text" as const, text: "Verification scan stopped." }] };
    }

    if (act === "status") {
      const { status, data } = await apiGet("/verify/status");
      if (status === 404) return { content: [{ type: "text" as const, text: "No verification scan has been run. Use action='start' to begin." }] };
      if (status !== 200) return { content: [{ type: "text" as const, text: `Status failed (${status}): ${data?.error ?? JSON.stringify(data)}` }] };
      const running = data.running ?? false;
      const lines: string[] = [];
      if (running) {
        const tot = data.total_to_update ?? 0;
        const done = data.updated ?? 0;
        lines.push(`Running  : yes  (${tot ? `${Math.floor(done * 100 / tot)}%` : "—"} complete)`);
      }
      lines.push(
        `Status   : ${data.status   ?? "?"}`,
        `Phase    : ${data.phase    ?? "?"}`,
        `Started  : ${data.started_at ?? "?"}`,
        `Updated  : ${data.last_update ?? "?"}`,
        `FS files : ${data.fs_files  ?? "?"}`,
        `Index    : ${data.index_docs ?? "?"} docs`,
        `Missing  : ${data.missing  ?? 0}`,
        `Stale    : ${data.stale    ?? 0}`,
        `Orphaned : ${data.orphaned ?? 0}`,
        `Re-indexed: ${data.updated ?? 0}`,
        `Deleted  : ${data.deleted  ?? 0} orphans removed`,
        `Errors   : ${data.errors   ?? 0}`,
      );
      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    }

    if (act === "start") {
      let collection: string, srcRoot: string;
      try { [collection, srcRoot] = getRoot(root); }
      catch (e: any) { return { content: [{ type: "text" as const, text: `Error: ${e.message}` }] }; }

      const { status, data } = await httpPost(API_PORT, "/verify/start", { root: root || "default", delete_orphans });
      if (status === 409) return { content: [{ type: "text" as const, text: "A verification scan is already running.\nUse action='status' to monitor, or action='stop' to cancel." }] };
      if (status !== 200) return { content: [{ type: "text" as const, text: `Start failed (${status}): ${data?.error ?? JSON.stringify(data)}` }] };
      return { content: [{ type: "text" as const, text: `Verification scan started.\nRoot      : '${root || "default"}' → ${srcRoot}\nCollection: ${collection}\nUse action='status' to monitor progress.` }] };
    }

    return { content: [{ type: "text" as const, text: `Unknown action: '${action}'. Use 'start', 'status', or 'stop'.` }] };
  }
);

// ── manage_service ────────────────────────────────────────────────────────────

server.tool(
  "manage_service",
  `Start, stop, restart, check status, or rebuild the code search service.

Manages the Docker container running the Python indexserver + Typesense.

Args:
  action: One of:
          "start"   — Start the Docker container.
          "stop"    — Stop the Docker container.
          "restart" — Restart the Docker container.
          "status"  — Show service status (document counts, watcher state).
          "rebuild" — Wipe the index and re-index everything from scratch.
                      Runs in the background; monitor with action='status'.`,
  { action: z.string().default("status") },
  async ({ action }) => {
    const VALID = ["start", "stop", "restart", "status", "rebuild"];
    const act = action.toLowerCase().trim();
    if (!VALID.includes(act)) {
      return { content: [{ type: "text" as const, text: `Unknown action: '${action}'. Valid: ${VALID.join(", ")}` }] };
    }

    if (act === "status") {
      try {
        const { status, data } = await apiGet("/status");
        if (status !== 200) throw new Error(`HTTP ${status}`);
        const lines = ["Service status:"];
        for (const [name, info] of Object.entries<any>(data.collections ?? {})) {
          const ndocs   = info.num_documents;
          const exists  = info.collection_exists;
          const warns   = info.schema_warnings ?? [];
          if (!exists) {
            lines.push(`  Root '${name}': not yet indexed — run manage_service(action='rebuild')`);
          } else if (warns.length) {
            lines.push(`  Root '${name}': ${ndocs?.toLocaleString()} docs  [SCHEMA OUTDATED — ${warns.join("; ")}]`);
          } else {
            lines.push(`  Root '${name}': ${ndocs?.toLocaleString()} docs  OK`);
          }
        }
        const w = data.watcher ?? {};
        lines.push(`Watcher: ${w.state ?? "unknown"}  queue depth: ${data.queue?.depth ?? 0}`);
        if (data.indexer?.running) lines.push(`Indexer: running  ${JSON.stringify(data.indexer.progress ?? {})}`);
        return { content: [{ type: "text" as const, text: lines.join("\n") }] };
      } catch (e: any) {
        return { content: [{ type: "text" as const, text: `Indexserver not reachable: ${e.message}\nTry: manage_service(action='start')` }] };
      }
    }

    if (act === "rebuild") {
      const results: string[] = [];
      for (const rootName of Object.keys(ROOTS)) {
        try {
          const { status, data } = await httpPost(API_PORT, "/index/start", { root: rootName, resethard: true });
          results.push(status === 200
            ? `Root '${rootName}': re-indexing started (${data.collection})`
            : `Root '${rootName}': failed (${status}) — ${data?.error ?? JSON.stringify(data)}`);
        } catch (e: any) {
          results.push(`Root '${rootName}': error — ${e.message}`);
        }
      }
      results.push("\nRe-indexing is running in the background. Use action='status' to monitor.");
      return { content: [{ type: "text" as const, text: results.join("\n") }] };
    }

    // start / stop / restart — Docker CLI
    const res = spawnSync("docker", [act, DOCKER_CONTAINER], { encoding: "utf-8", timeout: 30_000 });
    const out = ((res.stdout ?? "") + (res.stderr ?? "")).trim();
    if (res.status !== 0) {
      return { content: [{ type: "text" as const, text: `docker ${act} failed (exit ${res.status ?? "?"}): ${out}` }] };
    }
    return { content: [{ type: "text" as const, text: out || `Service '${act}' completed.` }] };
  }
);

// ── service_status ────────────────────────────────────────────────────────────

server.tool(
  "service_status",
  `Check whether the Typesense code search service is running.
Returns server health, document count per root, and watcher state.
If not running, returns instructions to start it.

Args:
  root: Named root to inspect (empty = show all configured roots).`,
  { root: z.string().default("") },
  async ({ root }) => {
    let st: any;
    try {
      const { status, data } = await apiGet("/status", 3_000);
      if (status !== 200) throw new Error(`HTTP ${status}`);
      st = data;
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Indexserver is NOT running.\nStart it with: ts start\nError: ${e.message}` }] };
    }

    const rootNames      = root ? [root] : Object.keys(ROOTS);
    const indexerRunning = st.indexer?.running ?? false;
    const lines          = [`Typesense  : ${st.typesense_ok !== false ? "ok" : "NOT OK"}`];

    for (const rootName of rootNames) {
      let collName: string;
      try { [collName] = getRoot(rootName); }
      catch (e: any) { lines.push(`Error: ${e.message}`); continue; }

      const info  = st.collections?.[rootName];
      const ndocs = info?.num_documents;
      const exists = info?.collection_exists ?? (ndocs != null);
      const warns  = info?.schema_warnings ?? [];
      if (!exists) {
        lines.push(indexerRunning
          ? `Root '${rootName}' (${collName}): indexing in progress`
          : `Root '${rootName}' (${collName}): not yet indexed — run: ts index`);
      } else if (warns.length) {
        lines.push(`Root '${rootName}' (${collName}): ${ndocs?.toLocaleString()} docs  [SCHEMA OUTDATED — ${warns.join("; ")}]`);
      } else {
        lines.push(`Root '${rootName}' (${collName}): ${ndocs?.toLocaleString()} docs`);
      }
    }

    return { content: [{ type: "text" as const, text: lines.join("\n") }] };
  }
);

// ── Start server ──────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
