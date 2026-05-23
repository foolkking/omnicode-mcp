/**
 * i18n manager — retroactive English ↔ 中文 translator.
 *
 * The dashboard's HTML is hard-coded in English (no `data-i18n` attributes
 * everywhere).  Instead of touching every template, this module scans the
 * rendered DOM and swaps known English snippets for their Chinese
 * equivalents.  Translation is reversible: when switching back to English
 * we re-traverse and swap the Chinese strings back.
 *
 * Coverage strategy:
 *   • TEXT[en→zh] map — phrase-level matches in element textContent / attrs.
 *   • Includes nav items, buttons, headings, table headers, labels,
 *     placeholders, titles, common modal copy.
 *   • MutationObserver re-translates dynamically inserted DOM (after a
 *     section is async-loaded by componentLoader).
 *
 * Public API:
 *   window.i18n.getLang()     -> 'en' | 'zh'
 *   window.i18n.setLang('zh') -> persists + re-translates
 *   window.i18n.toggle()      -> en <-> zh
 *   window.i18n.t(en)         -> returns localized string for current lang
 */
(function (root) {
    'use strict';

    var STORAGE_KEY = 'omnicode.lang';
    var VALID = ['en', 'zh'];

    // ----------------------------------------------------------------- dict
    // English source string → Chinese translation.
    // Keep entries reasonably specific (multi-word phrases) so we don't
    // accidentally rewrite "File" inside random tooltips.
    var EN_TO_ZH = {
        // Header / shell
        'Codebase Manager': '代码库管理器',
        'AI-Powered Development': 'AI 驱动开发',
        'Dashboard Overview': '控制板概览',
        'AI-Powered Development Dashboard': 'AI 驱动开发控制板',
        'Connecting...': '连接中...',
        'Connecting…': '连接中…',
        'Connected': '已连接',
        'Disconnected': '已断开',
        'Refresh': '刷新',
        'History': '历史',
        'Light': '浅色',
        'Dark': '深色',
        'System': '跟随系统',
        'Files:': '文件:',
        'Lines:': '行数:',
        'Memories:': '记忆:',
        'Branch:': '分支:',
        'Last Refresh:': '最后刷新:',
        'Never': '从未',
        'Loading dashboard...': '加载控制板...',
        'Processing...': '处理中...',

        // Sidebar nav
        'Dashboard': '控制板',
        'Search & Index': '搜索 & 索引',
        'File Operations': '文件操作',
        'Git & Sessions': 'Git & 会话',
        'Memory System': '记忆系统',
        'Project Explorer': '项目浏览器',
        'Code Graph Viewer': '代码图谱',
        'Directory Browser': '目录浏览器',
        'Working Directory': '工作目录',
        'Tool Call History': '工具调用记录',
        'System Logs': '系统日志',
        'Settings': '设置',
        'Model Providers': '模型提供方',
        'System Status': '系统状态',
        'API:': 'API:',
        'Directory:': '目录:',
        'Tool Calls:': '工具调用:',
        'Unknown': '未知',

        // Common controls
        'Save': '保存',
        'Cancel': '取消',
        'Delete': '删除',
        'Edit': '编辑',
        'Test': '测试',
        'Add': '添加',
        'Close': '关闭',
        'Search': '搜索',
        'Loading...': '加载中...',
        'Loading…': '加载中…',
        'Failed': '失败',
        'Success': '成功',
        'Error': '错误',
        'View': '查看',
        'Reload': '重新加载',
        'Apply': '应用',
        'Confirm': '确认',
        'Yes': '是',
        'No': '否',
        'Copy': '复制',
        'Run': '运行',
        'Reset': '重置',
        'Submit': '提交',
        'Refresh All': '全部刷新',

        // Dashboard cards
        'Project Statistics': '项目统计',
        'Symbols': '符号',
        'Code Chunks': '代码块',
        'Files': '文件',
        'Memories': '记忆',
        'Health': '健康',
        'Healthy': '健康',
        'Quick Actions': '快捷操作',
        'Recent Activity': '最近活动',
        'No activity yet': '暂无活动',
        'Current Branch:': '当前分支:',
        'Session Type:': '会话类型:',
        'Active Session': '活跃会话',
        'Regular Branch': '普通分支',
        'Manage Session': '管理会话',
        'Start Session': '开始会话',

        // Search section
        'Semantic Search': '语义搜索',
        'Text Search': '文本搜索',
        'Symbol Search': '符号搜索',
        'Index Codebase': '索引代码库',
        'Search Statistics': '搜索统计',
        'Search Results': '搜索结果',
        'No results found': '未找到结果',
        'View Code': '查看代码',
        'Indexed Files': '已索引文件',
        'Total Chunks': '代码块总数',
        'Last Indexed': '最后索引时间',
        'Index Size': '索引大小',
        'Query': '查询',
        'Top K': '返回数',
        'File Pattern': '文件模式',

        // Files section
        'Read Code': '读取代码',
        'Write File': '写入文件',
        'AI Edit': 'AI 编辑',
        'List Symbols': '列出符号',
        'AI-Assisted Editing': 'AI 辅助编辑',
        'Driven by smart router — describe the change and AI applies it': '由智能路由驱动 — 描述变更后由 AI 自动应用',
        'Target File': '目标文件',
        'Edit Instructions': '编辑指令',
        'Code Edit Sketch': '代码草稿',
        'Edit File': '编辑文件',
        'Edit Result': '编辑结果',
        'AI edit results will appear here...': 'AI 编辑结果将显示在这里...',
        'Edit Quality': '编辑质量',
        'Processing Time': '处理耗时',
        'Gemini Calls': 'Gemini 调用次数',
        'Warnings': '警告',
        'Edit Operations': '编辑操作',
        'Write Operations': '写入操作',
        'Suggested fixes': '建议修复',
        'Edit Failed': '编辑失败',
        'Write Failed': '写入失败',
        'File written:': '文件已写入:',
        'File edited:': '文件已编辑:',
        'Loaded': '已加载',
        'Use "// ... existing code ..." markers for unchanged regions': '使用 "// ... existing code ..." 标记未变更的部分',
        'Describe what you want to change': '描述您想要的修改',
        'e.g., Add error handling to process_data function': '例如:为 process_data 函数添加错误处理',

        // Git & Sessions
        'AI Session Management': 'AI 会话管理',
        'Git Operations': 'Git 操作',
        'Session Name': '会话名称',
        'Start': '开始',
        'End': '结束',
        'List All': '全部列表',
        'Switch': '切换',
        'Merge': '合并',
        'Available Sessions': '可用会话',
        'Active': '已激活',
        'Inactive': '未激活',
        'File path (optional)': '文件路径(可选)',
        'For single file operations': '用于针对单个文件的操作',
        'Commit Message': '提交信息',
        'For commit operations': '用于提交操作',
        'Status': '状态',
        'Branches': '分支',
        'Log': '日志',
        'Diff': '差异',
        'Tree': '树',
        'Git output will appear here...': 'Git 输出将显示在这里...',
        'Modified Files': '修改的文件',
        'Git operations and AI session development': 'Git 操作与 AI 驱动的开发会话',

        // Memory section
        'Store New Memory': '存储新记忆',
        'Store Memory': '存储记忆',
        'Search Memory': '搜索记忆',
        'Memory Browser': '记忆浏览器',
        'All Categories': '全部分类',
        'Learning': '学习',
        'Progress': '进展',
        'Preference': '偏好',
        'Mistake': '错误',
        'Solution': '方案',
        'Category': '分类',
        'Content': '内容',
        'Tags': '标签',
        'Importance': '重要程度',
        'Related Files': '相关文件',
        'Subcategory': '子分类',
        'Session': '会话',
        'Dedupe': '去重',
        'Edit Memory': '编辑记忆',
        'No memories found': '未找到记忆',
        'Memory updated': '记忆已更新',
        'Memory archived': '记忆已归档',
        'Update failed:': '更新失败:',
        'Content cannot be empty': '内容不能为空',

        // Logs
        'Live': '实时',
        'Pause': '暂停',
        'Stream connected': '日志流已连接',
        'Stream disconnected': '日志流已断开',
        'Filter:': '过滤:',
        'Clear': '清空',
        'Export': '导出',

        // Provider section
        'Add Provider': '添加提供方',
        'Edit Provider': '编辑提供方',
        'Provider Name': '提供方名称',
        'Model': '模型',
        'API Key': 'API 密钥',
        'API Base URL': 'API 基地址',
        'Provider Type': '提供方类型',
        'Group': '分组',
        'Enabled': '启用',
        'Built-in': '内置',
        'Description': '描述',
        'Active Routing Selections': '活跃路由分配',
        'Roles': '角色',
        'Apply Selections': '应用分配',
        'Reset Selections': '重置分配',
        'Test Provider': '测试提供方',
        'Pong': 'Pong',
        'Test failed': '测试失败',

        // Working directory
        'Current Working Directory': '当前工作目录',
        'Change Directory': '切换目录',
        'Browse': '浏览',
        'Path': '路径',
        'Set as Working Directory': '设为工作目录',

        // Settings
        'General': '通用',
        'Appearance': '外观',
        'About': '关于',
        'Version': '版本',
        'License': '许可证',

        // Misc / common phrases
        'Items per page:': '每页条数:',
        'Page:': '页码:',
        'of': '/',
        'Total:': '总计:',
        'Operation completed': '操作已完成',
        'Operation failed': '操作失败',
        'You are absolutely right': '你说得对',
    };

    // Reverse map (built once)
    var ZH_TO_EN = {};
    Object.keys(EN_TO_ZH).forEach(function (k) { ZH_TO_EN[EN_TO_ZH[k]] = k; });

    function pickMap(toLang) { return toLang === 'zh' ? EN_TO_ZH : ZH_TO_EN; }

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

    // Translate one string by trimming surrounding whitespace, looking it up,
    // and reapplying the original whitespace.  This way "Save  " and "Save"
    // both translate even if the HTML inserted a stray space.
    function translateText(s, map) {
        if (!s || typeof s !== 'string') return null;
        var trimmed = s.replace(/^\s+|\s+$/g, '');
        if (!trimmed) return null;
        if (map.hasOwnProperty(trimmed)) {
            var leading = s.match(/^\s*/)[0];
            var trailing = s.match(/\s*$/)[0];
            return leading + map[trimmed] + trailing;
        }
        return null;
    }

    function translateSubtree(scope, toLang) {
        scope = scope || root.document.body || root.document;
        var map = pickMap(toLang);

        // Skip these tags entirely — translating their text content would
        // break the page (e.g. <script>) or pollute structured data.
        var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };

        // 1. Walk all text nodes
        var walker = root.document.createTreeWalker(
            scope, NodeFilter.SHOW_TEXT, null, false
        );
        var node;
        while ((node = walker.nextNode())) {
            if (!node.parentNode) continue;
            if (SKIP_TAGS[node.parentNode.tagName]) continue;
            var translated = translateText(node.nodeValue, map);
            if (translated != null && translated !== node.nodeValue) {
                node.nodeValue = translated;
            }
        }

        // 2. Translate placeholders / titles / aria-label on form-like elements
        var els = scope.querySelectorAll(
            '[placeholder], [title], [aria-label], [data-tooltip]'
        );
        els.forEach(function (el) {
            ['placeholder', 'title', 'aria-label', 'data-tooltip'].forEach(function (attr) {
                var v = el.getAttribute(attr);
                if (!v) return;
                var t = translateText(v, map);
                if (t != null) el.setAttribute(attr, t);
            });
        });

        // 3. <option> values keep their value attribute, but their visible
        //    label is in textContent — already covered by step 1.
    }

    // Backwards-compat: a tiny dict-keyed t() for code that needs a
    // specific key (theme.toggleHint, etc).
    var KEYED = {
        en: {
            theme: {
                toggleHint: 'Click to switch theme (light → dark → system)',
                light: 'Light', dark: 'Dark', system: 'System',
            },
            lang: { toggleHint: 'Switch language' },
        },
        zh: {
            theme: {
                toggleHint: '点击切换主题(浅色 → 深色 → 跟随系统)',
                light: '浅色', dark: '深色', system: '跟随系统',
            },
            lang: { toggleHint: '切换语言' },
        },
    };
    function tKeyed(key) {
        var parts = String(key || '').split('.');
        var node = KEYED[current] || KEYED.en;
        for (var i = 0; i < parts.length; i++) {
            if (node == null || typeof node !== 'object') return null;
            node = node[parts[i]];
        }
        return typeof node === 'string' ? node : null;
    }

    function t(s) {
        // Two-arg overload: t('theme.toggleHint') → keyed lookup
        if (s && /^[a-z][a-zA-Z0-9_.]*$/.test(s)) {
            var keyed = tKeyed(s);
            if (keyed != null) return keyed;
        }
        // Otherwise treat as a literal English string
        var map = pickMap(current);
        var translated = translateText(s, map);
        return translated != null ? translated : s;
    }

    function refreshButtons() {
        var btns = root.document.querySelectorAll('[data-lang-toggle]');
        btns.forEach(function (btn) {
            var span = btn.querySelector('[data-lang-label]');
            if (span) span.textContent = current === 'zh' ? '中文' : 'EN';
            btn.title = tKeyed('lang.toggleHint') || 'Switch language';
        });
    }

    function setLang(lang) {
        if (VALID.indexOf(lang) < 0) return;
        if (lang === current) return;
        // Translate from current to target
        translateSubtree(root.document.body, lang);
        current = lang;
        writeStored(lang);
        try {
            root.document.documentElement.setAttribute(
                'lang', lang === 'zh' ? 'zh-Hans' : 'en'
            );
        } catch (e) { /* ignore */ }
        refreshButtons();
        try {
            root.dispatchEvent(new CustomEvent('languagechange', {
                detail: { lang: lang }
            }));
        } catch (e) { /* ignore */ }
    }

    function toggle() { setLang(current === 'en' ? 'zh' : 'en'); }

    // Public API
    root.i18n = {
        getLang: function () { return current; },
        setLang: setLang,
        toggle: toggle,
        t: t,
        applyTranslations: function () {
            translateSubtree(root.document.body, current);
        },
    };
    root.tr = t; // back-compat alias

    function bootstrap() {
        // On first paint, if user previously chose Chinese, translate the page.
        if (current === 'zh') {
            translateSubtree(root.document.body, 'zh');
            try {
                root.document.documentElement.setAttribute('lang', 'zh-Hans');
            } catch (e) { /* ignore */ }
        }
        refreshButtons();
    }

    if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }

    // Re-run on every dynamic section load
    root.document.addEventListener('sectionLoaded', function () {
        if (current === 'zh') {
            translateSubtree(root.document.body, 'zh');
        }
        refreshButtons();
    });

    // MutationObserver for async-rendered DOM (e.g. memory list, search
    // results).  Translates only NEWLY-added subtrees to avoid touching
    // already-translated text repeatedly.
    var pending = [];
    var scheduled = false;
    function flushPending() {
        scheduled = false;
        if (!pending.length || current !== 'zh') {
            pending = [];
            return;
        }
        var batch = pending.slice();
        pending = [];
        batch.forEach(function (n) {
            if (n && n.isConnected) {
                translateSubtree(n, 'zh');
            }
        });
    }
    try {
        var obs = new MutationObserver(function (mutations) {
            for (var i = 0; i < mutations.length; i++) {
                var added = mutations[i].addedNodes;
                for (var j = 0; j < added.length; j++) {
                    var n = added[j];
                    if (n.nodeType === 1) pending.push(n);
                }
            }
            if (!scheduled && pending.length) {
                scheduled = true;
                requestAnimationFrame(flushPending);
            }
        });
        obs.observe(root.document.body || root.document.documentElement, {
            childList: true, subtree: true,
        });
    } catch (e) { /* ignore on old browsers */ }
})(typeof window !== 'undefined' ? window : this);
