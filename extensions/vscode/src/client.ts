/**
 * Tiny HTTP client for the OmniCode-MCP server.
 *
 * Uses Node's built-in `http`/`https` modules so the extension has zero
 * runtime dependencies — VS Code refuses to install bundles that pull
 * in axios/node-fetch for a 200-line surface like ours.
 */

import * as http from "http";
import * as https from "https";
import { URL } from "url";
import * as vscode from "vscode";

export interface OmnicodeConfig {
    serverUrl: string;
    apiKey: string;
    confirmApplyPatch: boolean;
}

export interface ApiResult<T = any> {
    ok: boolean;
    status: number;
    body: T | null;
    error?: string;
}

/** Read the live extension settings each call so changes take effect
 *  without a window reload. */
export function readConfig(): OmnicodeConfig {
    const cfg = vscode.workspace.getConfiguration("omnicode");
    return {
        serverUrl: (cfg.get<string>("serverUrl") || "http://127.0.0.1:6789").replace(
            /\/+$/,
            ""
        ),
        apiKey: cfg.get<string>("apiKey") || "",
        confirmApplyPatch: cfg.get<boolean>("confirmApplyPatch", true),
    };
}

interface RequestOptions {
    method?: "GET" | "POST" | "DELETE";
    path: string;
    query?: Record<string, string | number | boolean | undefined>;
    body?: any;
    timeoutMs?: number;
}

function _buildUrl(base: string, path: string, query?: RequestOptions["query"]): URL {
    const u = new URL(base + path);
    if (query) {
        for (const [k, v] of Object.entries(query)) {
            if (v === undefined || v === null) continue;
            u.searchParams.set(k, String(v));
        }
    }
    return u;
}

export async function request<T = any>(opts: RequestOptions): Promise<ApiResult<T>> {
    const cfg = readConfig();
    const url = _buildUrl(cfg.serverUrl, opts.path, opts.query);

    const transport = url.protocol === "https:" ? https : http;
    const headers: Record<string, string> = {
        "Content-Type": "application/json",
        Accept: "application/json",
    };
    if (cfg.apiKey) {
        headers["X-API-Key"] = cfg.apiKey;
    }
    const payload = opts.body !== undefined ? JSON.stringify(opts.body) : null;
    if (payload) {
        headers["Content-Length"] = Buffer.byteLength(payload).toString();
    }

    return new Promise((resolve) => {
        const req = transport.request(
            {
                method: opts.method || "GET",
                hostname: url.hostname,
                port: url.port || (url.protocol === "https:" ? 443 : 80),
                path: url.pathname + url.search,
                headers,
            },
            (res) => {
                const chunks: Buffer[] = [];
                res.on("data", (c) => chunks.push(c));
                res.on("end", () => {
                    const text = Buffer.concat(chunks).toString("utf-8");
                    const status = res.statusCode || 0;
                    let body: T | null = null;
                    try {
                        body = text ? (JSON.parse(text) as T) : null;
                    } catch {
                        body = (text as unknown) as T;
                    }
                    resolve({
                        ok: status >= 200 && status < 300,
                        status,
                        body,
                        error: status >= 400 ? `HTTP ${status}` : undefined,
                    });
                });
            }
        );

        req.setTimeout(opts.timeoutMs || 15000, () => {
            req.destroy();
            resolve({ ok: false, status: 0, body: null, error: "request timed out" });
        });

        req.on("error", (err) => {
            resolve({ ok: false, status: 0, body: null, error: err.message });
        });

        if (payload) req.write(payload);
        req.end();
    });
}
