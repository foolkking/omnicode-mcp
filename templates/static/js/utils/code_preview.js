/**
 * Code preview modal utility — used by search results, graph viewer, file
 * symbol list, etc.
 *
 * Features
 * --------
 * - 85vh tall, scrollable, with line gutter parsed from the backend's
 *   ``with_line_numbers`` output (or rendered locally when absent).
 * - Highlights a target line and auto-scrolls to it.
 * - Optional syntax highlighting via highlight.js (loaded lazily on first
 *   open from a CDN) so we don't pay the bytes for users who never open the
 *   modal.
 * - "Open in editor" button that uses the ``vscode://file/<abs>:<line>``
 *   URL scheme, plus fallback handlers for cursor / IDEA / Sublime if the
 *   user opted in via localStorage.
 * - Copy button that strips the gutter so the user gets the bare source.
 *
 * The modal is rendered into ``document.body`` and kept as a singleton —
 * opening a new modal disposes the previous one.
 */
(function () {
    'use strict';

    const HLJS_CDN = 'https://cdn.jsdelivr.net/npm/highlight.js@11.9.0';
    let _hljsLoading = null;

    function _loadHighlightJs() {
        if (window.hljs) return Promise.resolve(window.hljs);
        if (_hljsLoading) return _hljsLoading;
        _hljsLoading = new Promise((resolve) => {
            // CSS — atom-one-dark works for both modes (we keep modal bg dark).
            const css = document.createElement('link');
            css.rel = 'stylesheet';
            css.href = HLJS_CDN + '/styles/atom-one-dark.min.css';
            document.head.appendChild(css);
            // Core
            const s = document.createElement('script');
            s.src = HLJS_CDN + '/highlight.min.js';
            s.onload = () => resolve(window.hljs || null);
            s.onerror = () => resolve(null);
            document.head.appendChild(s);
        });
        return _hljsLoading;
    }

    function _esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function _detectLanguage(filePath) {
        if (!filePath) return null;
        const ext = (filePath.match(/\.([a-z0-9]+)$/i) || [, ''])[1].toLowerCase();
        return {
            py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
            jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
            cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp',
            c: 'c', h: 'c',
            java: 'java', go: 'go', rs: 'rust',
            json: 'json', yaml: 'yaml', yml: 'yaml',
            html: 'xml', xml: 'xml', sql: 'sql', sh: 'bash', bash: 'bash',
            md: 'markdown',
        }[ext] || null;
    }

    function _absolutePath(file) {
        // The backend sends rel paths post _graphRelPath, but the modal can
        // also be invoked with absolute paths (e.g. from the file browser).
        // For "Open in editor" we need an absolute path: prepend the working
        // directory if the path looks relative.
        if (!file) return '';
        const isAbs = /^([a-zA-Z]:[\\/]|\/)/.test(file);
        if (isAbs) return file;
        const wd = window.__appWorkingDir || '';
        if (!wd) return file;
        const sep = (/[\\/]$/.test(wd)) ? '' : (wd.indexOf('\\') >= 0 ? '\\' : '/');
        return wd + sep + file;
    }

    function _editorUrl(scheme, file, line) {
        const abs = _absolutePath(file).replace(/\\/g, '/');
        // VS Code expects file:// triple-slash for absolute Windows paths
        // (URL: vscode://file/C:/path/to/file:42:1).  On *nix it's
        // vscode://file//abs/path:42:1.
        return scheme + '://file/' + abs + (line ? ':' + line : '');
    }

    function _openInEditor(file, line) {
        const scheme = window.localStorage.getItem('preferredEditor') || 'vscode';
        const url = _editorUrl(scheme, file, line);
        // Anchor click is more reliable than location.href for custom schemes.
        const a = document.createElement('a');
        a.href = url;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => a.remove(), 200);
        if (window.notifications) {
            notifications.info(`Opening ${file}${line ? ':' + line : ''} in ${scheme}…`);
        }
    }

    function showCodePreviewModal(filePath, content, options) {
        options = options || {};
        const existing = document.getElementById('codePreviewModal');
        if (existing) existing.remove();

        const rawLines = String(content || '').split('\n');
        const lnRegex = /^\s*(\d+)\s\|\s?(.*)$/;
        let startLine = options.startLine || 1;
        let detectedStart = null;
        const detectedLines = [];
        let allMatch = rawLines.length > 0;
        rawLines.forEach((ln) => {
            const m = ln.match(lnRegex);
            if (m) {
                detectedLines.push({ n: parseInt(m[1], 10), text: m[2] });
                if (detectedStart == null) detectedStart = parseInt(m[1], 10);
            } else {
                allMatch = false;
            }
        });
        let rows;
        if (allMatch && detectedLines.length > 0) {
            rows = detectedLines;
            startLine = detectedStart;
        } else {
            rows = rawLines.map((text, i) => ({ n: startLine + i, text }));
        }
        const endLine = rows.length > 0 ? rows[rows.length - 1].n : startLine;
        const highlight = options.highlightLine != null ? options.highlightLine : startLine;
        const language = options.language || _detectLanguage(filePath);

        const title = options.title || filePath;
        const subtitle = rows.length ? `lines ${startLine}–${endLine}${language ? ' · ' + language : ''}` : '';

        const modal = document.createElement('div');
        modal.id = 'codePreviewModal';
        modal.className = 'fixed inset-0 bg-black bg-opacity-60 z-[80] flex items-center justify-center p-4';
        modal.dataset.filePath = filePath || '';
        modal.dataset.highlightLine = String(highlight);

        modal.innerHTML = '' +
            '<div class="bg-white dark:bg-gray-900 rounded-lg shadow-2xl w-full max-w-6xl h-[85vh] flex flex-col">' +
            '  <div class="px-5 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between flex-shrink-0">' +
            '    <div class="min-w-0 mr-3">' +
            '      <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100 flex items-center truncate">' +
            '        <i class="fas fa-code text-indigo-500 mr-2 flex-shrink-0"></i>' +
            '        <code class="font-mono text-sm truncate">' + _esc(title) + '</code>' +
            '      </h3>' +
            (subtitle ? '<div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">' + _esc(subtitle) + '</div>' : '') +
            '    </div>' +
            '    <div class="flex items-center space-x-2 flex-shrink-0">' +
            '      <button onclick="codePreviewOpenInEditor()" class="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" title="Open in VS Code (vscode:// protocol)">' +
            '        <i class="fas fa-external-link-alt mr-1"></i>Open' +
            '      </button>' +
            '      <button onclick="codePreviewJumpToHighlight()" class="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" title="Jump to highlighted line">' +
            '        <i class="fas fa-crosshairs mr-1"></i>Jump' +
            '      </button>' +
            '      <button onclick="copyCodePreview()" class="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" title="Copy">' +
            '        <i class="fas fa-copy mr-1"></i>Copy' +
            '      </button>' +
            '      <button onclick="closeCodePreview()" class="text-gray-500 hover:text-gray-700 dark:text-gray-300 dark:hover:text-white px-2" title="Close">' +
            '        <i class="fas fa-times"></i>' +
            '      </button>' +
            '    </div>' +
            '  </div>' +
            '  <div id="codePreviewScroll" class="flex-1 overflow-auto bg-gray-900">' +
            '    <div id="codePreviewBody" class="text-xs font-mono text-gray-100 py-3">' +
            rows.map(r => {
                const isHi = (r.n === highlight);
                return '<div class="code-preview-row flex' + (isHi ? ' is-highlight bg-yellow-500/20' : '') + '" data-line="' + r.n + '">' +
                    '<span class="code-preview-gutter select-none w-14 flex-shrink-0 text-right pr-3 text-gray-500">' +
                    r.n + '</span>' +
                    '<span class="code-preview-text flex-1 whitespace-pre">' + _esc(r.text) + '</span>' +
                    '</div>';
            }).join('') +
            '    </div>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(modal);

        modal.addEventListener('click', (ev) => { if (ev.target === modal) closeCodePreview(); });
        document.addEventListener('keydown', _codePreviewKey);

        // Lazy-load highlight.js and apply syntax colouring per row.
        if (language) {
            _loadHighlightJs().then((hljs) => {
                if (!hljs) return;
                modal.querySelectorAll('.code-preview-text').forEach((span) => {
                    try {
                        const r = hljs.highlight(span.textContent, { language, ignoreIllegals: true });
                        span.innerHTML = r.value;
                    } catch (e) { /* ignore highlight failure for this line */ }
                });
            });
        }

        requestAnimationFrame(codePreviewJumpToHighlight);
    }

    function codePreviewJumpToHighlight() {
        const modal = document.getElementById('codePreviewModal');
        if (!modal) return;
        const hi = modal.querySelector('.code-preview-row.is-highlight');
        const scroller = modal.querySelector('#codePreviewScroll');
        if (hi && scroller) {
            const top = hi.offsetTop - (scroller.clientHeight / 3);
            scroller.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
        }
    }

    function codePreviewOpenInEditor() {
        const modal = document.getElementById('codePreviewModal');
        if (!modal) return;
        const file = modal.dataset.filePath;
        const line = parseInt(modal.dataset.highlightLine, 10) || 1;
        if (!file) {
            if (window.notifications) notifications.warning('No file path attached to this preview.');
            return;
        }
        _openInEditor(file, line);
    }

    function closeCodePreview() {
        const m = document.getElementById('codePreviewModal');
        if (m) m.remove();
        document.removeEventListener('keydown', _codePreviewKey);
    }

    function _codePreviewKey(ev) {
        if (ev.key === 'Escape') closeCodePreview();
    }

    function copyCodePreview() {
        const body = document.getElementById('codePreviewBody');
        if (!body) return;
        const rows = body.querySelectorAll('.code-preview-row');
        let txt;
        if (rows.length) {
            txt = Array.prototype.map.call(rows, r => {
                const t = r.querySelector('.code-preview-text');
                return t ? t.textContent : '';
            }).join('\n');
        } else {
            txt = body.textContent || '';
        }
        if (navigator.clipboard) {
            navigator.clipboard.writeText(txt).then(
                () => window.notifications && notifications.success('Copied to clipboard'),
                () => window.notifications && notifications.error('Copy failed')
            );
        }
    }

    // Expose globally — section scripts use the bare names.
    window.showCodePreviewModal = showCodePreviewModal;
    window.codePreviewJumpToHighlight = codePreviewJumpToHighlight;
    window.codePreviewOpenInEditor = codePreviewOpenInEditor;
    window.closeCodePreview = closeCodePreview;
    window.copyCodePreview = copyCodePreview;
})();
