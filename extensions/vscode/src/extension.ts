/**
 * OmniCode-MCP — thin VS Code surface (Wave 2 W2-8).
 *
 * Three commands only — by design we do not compete with Cursor /
 * Copilot / Continue. We just make the existing OmniCode HTTP
 * endpoints reachable from the editor's command palette:
 *
 *   1. OmniCode: Show Impact      → GET  /graph/impact, /graph/risk,
 *                                   /graph/related-tests
 *   2. OmniCode: Apply Patch      → POST /patch/preview + /patch/apply
 *   3. OmniCode: Capability Status→ GET  /capabilities, render in the
 *                                   status bar
 */

import * as vscode from "vscode";
import { request, readConfig } from "./client";

let _statusBar: vscode.StatusBarItem | undefined;
let _outputChannel: vscode.OutputChannel | undefined;

function _output(): vscode.OutputChannel {
    if (!_outputChannel) {
        _outputChannel = vscode.window.createOutputChannel("OmniCode-MCP");
    }
    return _outputChannel;
}

// ---------------------------------------------------------------------------
// 1. Show Impact
// ---------------------------------------------------------------------------
async function showImpact(): Promise<void> {
    // Prefer the symbol the cursor sits on when there's an active editor;
    // otherwise prompt for one.
    let symbol: string | undefined;
    const editor = vscode.window.activeTextEditor;
    if (editor && !editor.selection.isEmpty) {
        symbol = editor.document.getText(editor.selection).trim();
    }
    if (!symbol && editor) {
        const range = editor.document.getWordRangeAtPosition(editor.selection.active);
        if (range) symbol = editor.document.getText(range).trim();
    }
    symbol = await vscode.window.showInputBox({
        prompt: "Symbol to analyze (function / class / method)",
        value: symbol || "",
        ignoreFocusOut: true,
    });
    if (!symbol) return;

    await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: `OmniCode: impact for ${symbol}`,
            cancellable: false,
        },
        async () => {
            const [impactRes, riskRes, testsRes] = await Promise.all([
                request<any>({ path: "/graph/impact", query: { symbol, depth: 2 } }),
                request<any>({ path: "/graph/risk", query: { symbol } }),
                request<any>({ path: "/graph/related-tests", query: { symbol } }),
            ]);

            if (!impactRes.ok) {
                vscode.window.showErrorMessage(
                    `OmniCode impact failed: ${impactRes.error || "unknown error"}`
                );
                return;
            }

            const impact = impactRes.body?.result || {};
            const risk = riskRes.ok ? riskRes.body?.result || {} : {};
            const tests = testsRes.ok ? testsRes.body?.result || {} : {};

            // Render results into a Markdown preview panel.
            const md = _renderImpactMarkdown(symbol!, impact, risk, tests);
            const panel = vscode.window.createWebviewPanel(
                "omnicodeImpact",
                `OmniCode: ${symbol}`,
                vscode.ViewColumn.Beside,
                { enableScripts: false }
            );
            panel.webview.html = `<!doctype html><html><body style="font-family: sans-serif; padding: 16px; color: var(--vscode-foreground); background: var(--vscode-editor-background);">${_markdownToHtml(md)}</body></html>`;
        }
    );
}

function _renderImpactMarkdown(
    symbol: string,
    impact: any,
    risk: any,
    tests: any
): string {
    const lines: string[] = [];
    lines.push(`# Impact — \`${symbol}\``);
    lines.push("");
    lines.push("## Summary");
    lines.push(`- Blast radius: **${impact.total_blast_radius ?? "—"}**`);
    lines.push(`- Affected (callees): ${impact.affected_count ?? "—"}`);
    lines.push(`- Dependents (callers): ${impact.dependent_count ?? "—"}`);
    lines.push(`- Files involved: ${impact.files_count ?? "—"}`);
    if (risk?.risk_level) {
        lines.push(
            `- Risk: **${String(risk.risk_level).toUpperCase()}** (score ${(
                risk.risk_score ?? 0
            ).toFixed?.(2) || risk.risk_score})`
        );
        if (risk.advisory) lines.push(`  > ${risk.advisory}`);
    }
    lines.push("");

    if ((impact.affected_symbols || []).length) {
        lines.push("## Affected (callees)");
        for (const s of (impact.affected_symbols || []).slice(0, 25)) {
            lines.push(`- \`${s}\``);
        }
        lines.push("");
    }
    if ((impact.dependent_symbols || []).length) {
        lines.push("## Dependents (callers)");
        for (const s of (impact.dependent_symbols || []).slice(0, 25)) {
            lines.push(`- \`${s}\``);
        }
        lines.push("");
    }
    if ((impact.files_involved || []).length) {
        lines.push("## Files involved");
        for (const f of (impact.files_involved || []).slice(0, 25)) {
            lines.push(`- ${f}`);
        }
        lines.push("");
    }
    if ((tests?.test_files || []).length) {
        lines.push("## Suggested tests");
        for (const f of (tests.test_files || []).slice(0, 15)) {
            lines.push(`- ${f}`);
        }
        if ((tests.suggested_commands || []).length) {
            lines.push("");
            lines.push("```bash");
            for (const c of tests.suggested_commands) lines.push(c);
            lines.push("```");
        }
    }
    return lines.join("\n");
}

function _markdownToHtml(md: string): string {
    // Keep the renderer trivial — no external markdown lib pulled in.
    // Headers, code blocks, list bullets, inline code only.
    let html = md
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    html = html.replace(/^### (.*)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.*)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.*)$/gm, "<h1>$1</h1>");
    html = html.replace(
        /```(\w*)\n([\s\S]*?)```/g,
        '<pre style="background:#1e1e1e;color:#ddd;padding:8px;border-radius:4px;overflow:auto;"><code>$2</code></pre>'
    );
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/^- (.*)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
    html = html.replace(/^> (.*)$/gm, '<blockquote>$1</blockquote>');
    html = html.replace(/\n\n/g, "<br><br>");
    return html;
}

// ---------------------------------------------------------------------------
// 2. Apply Patch
// ---------------------------------------------------------------------------
async function applyPatch(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage("Open the file you want to patch first.");
        return;
    }
    const wsFolder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
    if (!wsFolder) {
        vscode.window.showWarningMessage(
            "OmniCode patch requires a file inside an opened workspace."
        );
        return;
    }
    const relPath = vscode.workspace
        .asRelativePath(editor.document.uri, false)
        .replace(/\\/g, "/");

    const newContent = await vscode.window.showInputBox({
        prompt: `Replace the entire content of ${relPath} with:`,
        value: editor.document.getText(),
        ignoreFocusOut: true,
        // Single-line input becomes too painful for full-file replacements,
        // but it's fine for the smallest "rename a constant" workflow.
        // Power users should use the Web Console for bigger edits.
    });
    if (newContent === undefined) return;

    // Step 1 — preview.
    const previewRes = await request<any>({
        method: "POST",
        path: "/patch/preview",
        body: { file_path: relPath, content: newContent },
    });
    if (!previewRes.ok) {
        vscode.window.showErrorMessage(
            `Preview failed: ${previewRes.error || "unknown"}`
        );
        return;
    }
    const diff = previewRes.body?.result?.diff || "(no diff)";
    _output().show(true);
    _output().appendLine("=== /patch/preview ===");
    _output().appendLine(diff);

    const cfg = readConfig();
    if (cfg.confirmApplyPatch) {
        const choice = await vscode.window.showWarningMessage(
            `Apply patch to ${relPath}? See "OmniCode-MCP" output for the diff.`,
            { modal: true },
            "Apply",
            "Cancel"
        );
        if (choice !== "Apply") return;
    }

    // Step 2 — apply.
    const applyRes = await request<any>({
        method: "POST",
        path: "/patch/apply",
        body: { file_path: relPath, content: newContent },
    });
    if (!applyRes.ok) {
        vscode.window.showErrorMessage(
            `Apply failed: ${applyRes.error || "unknown"}`
        );
        return;
    }
    const session = applyRes.body?.result?.session_id || "?";
    vscode.window.showInformationMessage(
        `OmniCode: patch applied (session ${session.slice(0, 8)})`
    );
}

// ---------------------------------------------------------------------------
// 3. Capability Status
// ---------------------------------------------------------------------------
async function refreshStatusBar(): Promise<void> {
    if (!_statusBar) return;
    const res = await request<any>({ path: "/capabilities" });
    if (!res.ok || !res.body?.result) {
        _statusBar.text = "$(circle-slash) OmniCode: offline";
        _statusBar.tooltip = res.error || "Cannot reach OmniCode-MCP server";
        _statusBar.backgroundColor = new vscode.ThemeColor(
            "statusBarItem.warningBackground"
        );
        return;
    }
    const result = res.body.result;
    const total = result.total || 0;
    const available = result.available || 0;
    _statusBar.text =
        available === total
            ? `$(check) OmniCode ${available}/${total}`
            : `$(warning) OmniCode ${available}/${total}`;
    _statusBar.backgroundColor = undefined;
    const lines = ["OmniCode-MCP capability fingerprint:"];
    for (const c of result.capabilities || []) {
        lines.push(
            `  ${c.available ? "✓" : "✗"} ${c.capability} — ${c.detail || c.backend || ""}`
        );
    }
    _statusBar.tooltip = lines.join("\n");
}

async function showCapabilityStatus(): Promise<void> {
    await refreshStatusBar();
    const res = await request<any>({ path: "/capabilities" });
    if (!res.ok) {
        vscode.window.showErrorMessage(`Capabilities call failed: ${res.error}`);
        return;
    }
    const result = res.body?.result || {};
    const items = (result.capabilities || []).map((c: any) => ({
        label: `${c.available ? "$(check)" : "$(circle-slash)"} ${c.capability}`,
        description: c.backend || "",
        detail: c.detail || "",
    }));
    await vscode.window.showQuickPick(items, {
        placeHolder: `OmniCode capabilities — ${result.available}/${result.total} available`,
    });
}

// ---------------------------------------------------------------------------
// activate / deactivate
// ---------------------------------------------------------------------------
export function activate(context: vscode.ExtensionContext): void {
    _statusBar = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Right,
        100
    );
    _statusBar.command = "omnicode.capabilityStatus";
    _statusBar.text = "$(loading~spin) OmniCode";
    _statusBar.show();
    context.subscriptions.push(_statusBar);

    context.subscriptions.push(
        vscode.commands.registerCommand("omnicode.showImpact", showImpact),
        vscode.commands.registerCommand("omnicode.applyPatch", applyPatch),
        vscode.commands.registerCommand(
            "omnicode.capabilityStatus",
            showCapabilityStatus
        )
    );

    // Refresh the status bar at startup + every 60 s.
    refreshStatusBar();
    const timer = setInterval(refreshStatusBar, 60_000);
    context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

export function deactivate(): void {
    _statusBar?.dispose();
    _outputChannel?.dispose();
}
