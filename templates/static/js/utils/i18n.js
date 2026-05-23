/**
 * i18n manager — minimal English / Chinese toggle.
 *
 * - Persists choice to localStorage under `omnicode.lang`.
 * - Tracks system preference via navigator.language.
 * - Sets `lang="zh"` / `lang="en"` on <html>.
 * - Auto-translates DOM nodes carrying `data-i18n="some.key"` and
 *   `data-i18n-placeholder="some.key"`.
 * - Re-runs translation whenever a section is loaded
 *   (`sectionLoaded` event, fired by componentLoader).
 *
 * Globals:
 *   window.i18n.getLang()     -> 'en' | 'zh'
 *   window.i18n.setLang(lang) -> persists + applies + re-translates
 *   window.i18n.toggle()      -> en <-> zh
 *   window.i18n.t(key, ...args) -> string  (basic {0}/{1} substitution)
 */
(function (root) {
    'use strict';

    var STORAGE_KEY = 'omnicode.lang';
    var VALID = ['en', 'zh'];

    // ----------------------------------------------------------------- dict
    // Keep this small and pragmatic.  Every key has both an English and a
    // Chinese rendering.  Adding a new key:
    //   1. Drop it into both DICT.en and DICT.zh below.
    //   2. Add `data-i18n="my.new.key"` to the HTML element.
    var DICT = {
        en: {
            common: {
                refresh: 'Refresh', save: 'Save', cancel: 'Cancel', edit: 'Edit',
                delete: 'Delete', loading: 'Loading…', error: 'Error',
                success: 'Success', confirm: 'Confirm', close: 'Close',
                copy: 'Copy', search: 'Search', back: 'Back', test: 'Test',
            },
            header: {
                connected: 'Connected', disconnected: 'Disconnected',
                connecting: 'Connecting…',
                refresh: 'Refresh', history: 'History',
            },
            theme: {
                light: 'Light', dark: 'Dark', system: 'System',
                toggleHint: 'Click to switch theme (light → dark → system)',
            },
            lang: {
                en: 'English', zh: '中文',
                toggleHint: 'Switch language',
            },
            nav: {
                dashboard: 'Dashboard', search: 'Search & Index',
                files: 'File Operations', git: 'Git & Sessions',
                memory: 'Memory System', project: 'Project Explorer',
                graphViewer: 'Code Graph Viewer',
                directory: 'Directory Browser',
                workingDirectory: 'Working Directory',
                toolHistory: 'Tool Call History',
                logs: 'System Logs', settings: 'Settings',
                providers: 'Model Providers',
            },
            stats: {
                files: 'Files', lines: 'Lines', memories: 'Memories',
                branch: 'Branch', symbols: 'Symbols', chunks: 'Code Chunks',
                lastIndexed: 'Last Indexed',
            },
        },
        zh: {
            common: {
                refresh: '刷新', save: '保存', cancel: '取消', edit: '编辑',
                delete: '删除', loading: '加载中…', error: '错误',
                success: '成功', confirm: '确认', close: '关闭',
                copy: '复制', search: '搜索', back: '返回', test: '测试',
            },
            header: {
                connected: '已连接', disconnected: '已断开',
                connecting: '连接中…',
                refresh: '刷新', history: '历史',
            },
            theme: {
                light: '浅色', dark: '深色', system: '跟随系统',
                toggleHint: '点击切换主题(浅色 → 深色 → 跟随系统)',
            },
            lang: {
                en: 'English', zh: '中文',
                toggleHint: '切换语言',
            },
            nav: {
                dashboard: '控制板', search: '搜索 & 索引',
                files: '文件操作', git: 'Git & 会话',
                memory: '记忆系统', project: '项目浏览器',
                graphViewer: '代码图谱',
                directory: '目录浏览器',
                workingDirectory: '工作目录',
                toolHistory: '工具调用记录',
                logs: '系统日志', settings: '设置',
                providers: '模型提供方',
            },
            stats: {
                files: '文件', lines: '行数', memories: '记忆',
                branch: '分支', symbols: '符号总数', chunks: '代码块',
                lastIndexed: '最后索引',
            },
        },
    };

    // -------------------------------------------------------------- helpers
    function readStored() {
        try {
            var v = root.localStorage && root.localStorage.getItem(STORAGE_KEY);
            return VALID.indexOf(v) >= 0 ? v : null;
        } catch (e) { return null; }
    }
    function writeStored(v) {
        try { root.localStorage && root.localStorage.setItem(STORAGE_KEY, v); }
        catch (e) { /* ignore */ }
    }
    function detectSystem() {
        try {
            var lang = (root.navigator.language || 'en').toLowerCase();
            return lang.startsWith('zh') ? 'zh' : 'en';
        } catch (e) { return 'en'; }
    }

    var current = readStored() || detectSystem();
    if (VALID.indexOf(current) < 0) current = 'en';

    function lookup(key) {
        if (!key) return key;
        var parts = String(key).split('.');
        var node = DICT[current] || DICT.en;
        for (var i = 0; i < parts.length; i++) {
            if (node == null || typeof node !== 'object') return null;
            node = node[parts[i]];
        }
        if (typeof node !== 'string') {
            // Fallback to English if the key is missing in the active dict
            node = DICT.en;
            for (var j = 0; j < parts.length; j++) {
                if (node == null || typeof node !== 'object') return null;
                node = node[parts[j]];
            }
        }
        return typeof node === 'string' ? node : null;
    }

    function format(template, args) {
        if (!template || !args || !args.length) return template;
        return template.replace(/\{(\d+)\}/g, function (_m, idx) {
            var v = args[parseInt(idx, 10)];
            return v == null ? '' : String(v);
        });
    }

    function t(key) {
        var args = Array.prototype.slice.call(arguments, 1);
        var s = lookup(key);
        return s == null ? key : format(s, args);
    }

    function applyTranslations(scope) {
        scope = scope || root.document;
        // text content
        var nodes = scope.querySelectorAll('[data-i18n]');
        nodes.forEach(function (el) {
            var key = el.getAttribute('data-i18n');
            var s = lookup(key);
            if (s == null) return;
            // Preserve any leading icon (<i class="fas...">) if present
            var firstIcon = el.querySelector(':scope > i');
            if (firstIcon) {
                // Keep icon, replace only the trailing text
                var hasText = false;
                Array.prototype.forEach.call(el.childNodes, function (n) {
                    if (n.nodeType === 3) { n.textContent = ' ' + s; hasText = true; }
                });
                if (!hasText) {
                    el.appendChild(root.document.createTextNode(' ' + s));
                }
            } else {
                el.textContent = s;
            }
        });
        // placeholders
        var phs = scope.querySelectorAll('[data-i18n-placeholder]');
        phs.forEach(function (el) {
            var key = el.getAttribute('data-i18n-placeholder');
            var s = lookup(key);
            if (s != null) el.placeholder = s;
        });
        // titles
        var tts = scope.querySelectorAll('[data-i18n-title]');
        tts.forEach(function (el) {
            var key = el.getAttribute('data-i18n-title');
            var s = lookup(key);
            if (s != null) el.title = s;
        });
    }

    function setLang(lang) {
        if (VALID.indexOf(lang) < 0) return;
        current = lang;
        writeStored(lang);
        try {
            root.document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-Hans' : 'en');
        } catch (e) { /* ignore */ }
        applyTranslations();
        refreshButtons();
        try {
            root.dispatchEvent(new CustomEvent('languagechange', { detail: { lang: lang } }));
        } catch (e) { /* ignore */ }
    }

    function toggle() {
        setLang(current === 'en' ? 'zh' : 'en');
    }

    function refreshButtons() {
        var btns = root.document.querySelectorAll('[data-lang-toggle]');
        btns.forEach(function (btn) {
            var span = btn.querySelector('[data-lang-label]');
            if (span) span.textContent = current === 'zh' ? '中文' : 'EN';
            var i = btn.querySelector('i');
            if (i && !i.classList.contains('fa-language')) {
                i.className = 'fas fa-language mr-1.5';
            }
            btn.title = lookup('lang.toggleHint') || 'Switch language';
        });
    }

    // Public API
    root.i18n = {
        getLang: function () { return current; },
        setLang: setLang,
        toggle: toggle,
        t: t,
        applyTranslations: applyTranslations,
    };
    // Backwards-compat alias used by some sections
    root.tr = t;

    // Apply on first paint
    if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', function () {
            applyTranslations();
            refreshButtons();
            try {
                root.document.documentElement.setAttribute(
                    'lang', current === 'zh' ? 'zh-Hans' : 'en'
                );
            } catch (e) { /* ignore */ }
        });
    } else {
        applyTranslations();
        refreshButtons();
    }
    // Re-run on every dynamic section load + every theme button refresh
    root.document.addEventListener('sectionLoaded', function () {
        applyTranslations();
        refreshButtons();
    });

    // MutationObserver: catches inline scripts that inject DOM after a
    // sectionLoaded fires (e.g. async list rendering).  Throttled to one
    // sweep per animation frame so heavy DOM churn doesn't melt CPU.
    var pending = false;
    try {
        var obs = new MutationObserver(function () {
            if (pending) return;
            pending = true;
            requestAnimationFrame(function () {
                pending = false;
                applyTranslations();
            });
        });
        obs.observe(root.document.body || root.document.documentElement, {
            childList: true, subtree: true,
        });
    } catch (e) { /* ignore on old browsers */ }
})(typeof window !== 'undefined' ? window : this);
