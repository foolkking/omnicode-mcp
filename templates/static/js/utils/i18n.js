/**
 * i18n manager — retroactive English ↔ 中文 translator.
 *
 * Strategy: scan the rendered DOM and swap known English snippets for their
 * Chinese equivalents.  Entirely string-keyed so existing templates work
 * without `data-i18n` annotations.
 *
 * Public API:
 *   window.i18n.getLang()     -> 'en' | 'zh'
 *   window.i18n.setLang('zh') -> persists + re-translates
 *   window.i18n.toggle()      -> en <-> zh
 *   window.i18n.t(s)          -> translated copy of `s` for current lang
 */
(function (root) {
    'use strict';

    var STORAGE_KEY = 'omnicode.lang';
    var VALID = ['en', 'zh'];

    // ----------------------------------------------------------------- dict
    // Keep this list synced with templates.  Use _extract_strings.py to
    // regenerate the canonical English list.
    var EN_TO_ZH = {
        // === Header / shell ===
        'Codebase Manager': '代码库管理器',
        'Codebase Manager Dashboard': '代码库管理器控制板',
        'Codebase Manager - AI-Powered Development Dashboard': '代码库管理器 - AI 驱动开发控制板',
        'AI-Powered Development': 'AI 驱动开发',
        'AI-Powered Development Dashboard': 'AI 驱动开发控制板',
        'AI-Powered Development Dashboard - All systems operational': 'AI 驱动开发控制板 - 所有系统运行正常',
        'AI-powered codebase management system with:': 'AI 驱动的代码库管理系统:',
        'Welcome to Codebase Manager': '欢迎使用代码库管理器',
        'Dashboard Overview': '控制板概览',
        'Dashboard': '控制板',
        'Dashboard Settings': '控制板设置',
        'Settings & Preferences': '设置与偏好',
        'Configure dashboard behavior and preferences': '配置控制板行为和偏好',
        'Connecting...': '连接中...',
        'Connecting…': '连接中…',
        'Connected': '已连接',
        'Disconnected': '已断开',
        'Refresh': '刷新',
        'Refresh All': '全部刷新',
        'Refresh Stats': '刷新统计',
        'Refresh Interval (seconds)': '刷新间隔(秒)',
        'Auto-Refresh': '自动刷新',
        'Automatically refresh dashboard every 30s': '每 30 秒自动刷新控制板',
        'History': '历史',
        'Light': '浅色',
        'Dark': '深色',
        'System': '跟随系统',
        'EN': 'EN',
        'Files:': '文件:',
        'Lines:': '行数:',
        'Memories:': '记忆:',
        'Branch:': '分支:',
        'Loading dashboard...': '加载控制板...',
        'Processing...': '处理中...',
        'Just now': '刚刚',
        'Never': '从未',
        'Last Updated': '最后更新',

        // === Sidebar nav (and short tile headlines) ===
        'Search & Index': '搜索 & 索引',
        'Search & Index Management': '搜索与索引管理',
        'Semantic search, text search, symbol lookup, and index management': '语义搜索、文本搜索、符号查找与索引管理',
        'File Operations': '文件操作',
        'Read, write, and AI-assisted editing of code files': '读取、写入和 AI 辅助编辑代码文件',
        'Git & Sessions': 'Git & 会话',
        'Git & AI Sessions': 'Git & AI 会话',
        'Git Sessions': 'Git 会话',
        'Git automation & sessions': 'Git 自动化与会话',
        'Git operations and AI-powered development sessions': 'Git 操作与 AI 驱动的开发会话',
        'Git operations and AI session development': 'Git 操作与 AI 会话开发',
        'Memory System': '记忆系统',
        'Memory system for context': '上下文记忆系统',
        'Store and retrieve project knowledge across sessions': '跨会话存储和检索项目知识',
        'Project Explorer': '项目浏览器',
        'View project structure, dependencies, and metadata': '查看项目结构、依赖与元数据',
        'Code Graph Viewer': '代码图谱',
        'Interactive D3 visualisation of call inheritance graphs': 'D3 交互式可视化调用 / 继承图',
        'Directory Browser': '目录浏览器',
        'Browse and explore directory structure with metadata': '浏览目录结构与元数据',
        'Working Directory': '工作目录',
        'Working Directory Manager': '工作目录管理器',
        'Dynamic working directory management': '动态工作目录管理',
        'Dynamically change and manage your project working directory': '动态切换和管理项目工作目录',
        'Tool Call History': '工具调用记录',
        'Tool History': '工具历史',
        'Tool call recording & analysis': '工具调用记录与分析',
        'System Logs': '系统日志',
        'System Logs & Monitoring': '系统日志与监控',
        'Live System Logs': '实时系统日志',
        'Detailed logging of all system operations and performance metrics': '所有系统操作与性能指标的详细日志',
        'Settings': '设置',
        'Model Providers': '模型提供方',
        'System Status': '系统状态',
        'API:': 'API:',
        'Directory:': '目录:',
        'Tool Calls:': '工具调用:',
        'Unknown': '未知',
        'Initialized Services': '已初始化服务',
        'NEW': '新',

        // === Common controls ===
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
        'Warning': '警告',
        'Info': '信息',
        'Debug': '调试',
        'View': '查看',
        'View All': '查看全部',
        'Reload': '重新加载',
        'Apply': '应用',
        'Confirm': '确认',
        'Yes': '是',
        'No': '否',
        'Copy': '复制',
        'Copy path': '复制路径',
        'Run': '运行',
        'Reset': '重置',
        'Submit': '提交',
        'Validate': '验证',
        'Browse': '浏览',
        'Browse Directory': '浏览目录',
        'Browse Options': '浏览选项',
        'Open in file explorer': '在文件管理器中打开',
        'Type': '类型',
        'Status': '状态',
        'Categories': '分类',
        'Optional': '(可选)',
        'Auto-detect': '自动检测',
        'Critical': '严重',
        'Important': '重要',
        'All Levels': '全部等级',
        'All Statuses': '全部状态',
        'All Types': '全部类型',
        'All Routes': '全部路由',
        'All Components': '全部组件',
        'All Categories': '全部分类',
        'Errors Only': '仅错误',
        'Files Only': '仅文件',
        'Success Only': '仅成功',

        // === Dashboard cards ===
        'Project Statistics': '项目统计',
        'Project Stats': '项目统计',
        'Project Info': '项目信息',
        'Project Information': '项目信息',
        'Project files: ': '项目文件:',
        'Symbols': '符号',
        'Symbols Found': '已发现符号',
        'Code Chunks': '代码块',
        'Files': '文件',
        'Memories': '记忆',
        'Total Memories': '记忆总数',
        'Total Calls': '总调用数',
        'Health': '健康',
        'Healthy': '健康',
        'System Health': '系统健康',
        'Quick Actions': '快捷操作',
        'Recent Activity': '最近活动',
        'Recent Tool Calls': '最近工具调用',
        'Activity Feed': '动态信息流',
        'No activity yet': '暂无活动',
        'Current': '当前',
        'Current Branch': '当前分支',
        'Current Branch:': '当前分支:',
        'Current Directory': '当前目录',
        'Current Working Directory': '当前工作目录',
        'Session Type:': '会话类型:',
        'Session Status': '会话状态',
        'Active': '已激活',
        'Active Session': '活跃会话',
        'Regular Branch': '普通分支',
        'Manage Session': '管理会话',
        'Start Session': '开始会话',
        'API Calls:': 'API 调用:',
        'Avg Duration': '平均耗时',
        'Avg Response:': '平均响应:',
        'Avg Quality Score': '平均质量分',
        'Error Rate:': '错误率:',
        'Success Rate': '成功率',
        'Success Rate:': '成功率:',
        'Top Components': '主要组件',
        'Tool Activity': '工具活动',
        'Performance': '性能',
        'Log Distribution': '日志分布',
        'API Configuration': 'API 配置',
        'API Route Usage Statistics': 'API 路由使用统计',
        'Architecture': '架构',
        'Built with FastAPI, Tailwind CSS, and vanilla JavaScript': '基于 FastAPI、Tailwind CSS 和原生 JavaScript 构建',
        'Version 2.0 - Component Architecture': '版本 2.0 - 组件架构',

        // === Search section ===
        'Semantic Search': '语义搜索',
        'Semantic Query': '语义查询',
        'Semantic code search': '语义代码搜索',
        'Search Semantically': '语义搜索',
        'Text Search': '文本搜索',
        'Search Text': '搜索文本',
        'Text to Find': '查找文本',
        'Symbol Search': '符号搜索',
        'Search Symbols': '搜索符号',
        'Search Code': '搜索代码',
        'Search Query': '搜索查询',
        'Search Memories': '搜索记忆',
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
        'File Path': '文件路径',
        'File Path (Optional)': '文件路径(可选)',
        'Symbol Name': '符号名',
        'Symbol Name (Optional)': '符号名(可选)',
        'Symbol Type': '符号类型',
        'Start Line': '起始行',
        'End Line': '结束行',
        'Limit': '上限',
        'Max Depth': '最大深度',
        'Max Results': '最多结果数',
        'Max files:': '最大文件数:',
        'Max nodes:': '最大节点数:',
        'Hard cap on rendered nodes; the largest hubs are kept first.': '渲染节点硬上限,优先保留度数最高的中心节点。',
        'Scope:': '范围:',
        'Search:': '搜索:',
        'Use Regex': '使用正则',
        'Case Sensitive': '区分大小写',
        'Fuzzy Match': '模糊匹配',
        'Class': '类',
        'Classes and interfaces': '类与接口',
        'Function': '函数',
        'Functions and methods': '函数与方法',
        'Interface': '接口',
        'Enum': '枚举',
        'Variable declarations': '变量声明',
        'Types and enums': '类型与枚举',
        'What are symbols?': '什么是符号?',
        'Describe what you\'re looking for in natural language': '用自然语言描述你想找的内容',
        'Search for exact text...': '搜索精确文本...',
        'Search memories...': '搜索记忆...',
        'Search logs...': '搜索日志...',
        'Search tool calls...': '搜索工具调用...',
        'Results will appear here...': '结果将显示在这里...',
        'Symbols will appear here...': '符号将显示在这里...',
        'Call graph': '调用图',
        'Inheritance': '继承',
        'Fit': '适配',
        'symbol name...': '符号名...',
        'function or class name': '函数或类名',
        'function, class, or variable name': '函数、类或变量名',

        // === Files / Edit / Write ===
        'Read Code': '读取代码',
        'Code Content': '代码内容',
        'Code Preview': '代码预览',
        'Write File': '写入文件',
        'Write File with Quality Check': '带质量检查的写入',
        'Write Result': '写入结果',
        'AI Edit': 'AI 编辑',
        'AI Edit File': 'AI 编辑文件',
        'AI-Assisted Editing': 'AI 辅助编辑',
        'AI-assisted editing': 'AI 辅助编辑',
        'Edit Files': '编辑文件',
        'Edit Instructions': '编辑指令',
        'Code Edit (Sketch)': '代码编辑(草稿)',
        'Code Edit Sketch': '代码草稿',
        'Edit File': '编辑文件',
        'Edit Result': '编辑结果',
        'AI edit results will appear here...': 'AI 编辑结果将显示在这里...',
        'Edit Quality': '编辑质量',
        'Processing Time': '处理耗时',
        'Gemini Calls': 'Gemini 调用次数',
        'Warnings': '警告',
        'Errors': '错误',
        'Edit Operations': '编辑操作',
        'Write Operations': '写入操作',
        'List Symbols': '列出符号',
        'List All Symbols': '列出所有符号',
        'Suggested fixes': '建议修复',
        'Edit Failed': '编辑失败',
        'Write Failed': '写入失败',
        'Loaded': '已加载',
        'Use "// ... existing code ..." markers for unchanged regions': '使用 "// ... existing code ..." 标记未变更的部分',
        'Use to mark unchanged sections': '用于标记未变更的部分',
        'Describe what you want to change': '描述您想要的修改',
        'Describe what this file does': '描述这个文件做什么',
        'e.g., Add error handling to process_data function': '例如:为 process_data 函数添加错误处理',
        'e.g., function that processes user data': '例如:处理用户数据的函数',
        'Powered by Gemini - Specify changes and AI will apply them intelligently': '由 Gemini 驱动 - 描述变更后由 AI 智能应用',
        'Enter code content...': '输入代码内容...',
        'Show Line Numbers': '显示行号',
        'Show Hidden Files': '显示隐藏文件',
        'Show File Metadata': '显示文件元数据',
        'Show Tree View': '显示树视图',
        'Show Timestamps': '显示时间戳',
        'Display timestamps in API responses': '在 API 响应中显示时间戳',
        'Compact Mode': '紧凑模式',
        'Reduce spacing and padding': '减小间距和内边距',
        'Auto-scroll': '自动滚动',
        'Language (Auto-detected)': '语言(自动检测)',
        'Python': 'Python',
        'JavaScript': 'JavaScript',
        'TypeScript': 'TypeScript',
        'JSON': 'JSON',
        'CSV': 'CSV',
        'Export': '导出',
        'Export as CSV': '导出为 CSV',
        'Export as JSON': '导出为 JSON',
        'Purpose (Optional)': '目的(可选)',

        // === Git & Sessions ===
        'AI Session Management': 'AI 会话管理',
        'Git Operations': 'Git 操作',
        'Git Session': 'Git 会话',
        'Session Name': '会话名称',
        'Session info will appear here...': '会话信息将显示在这里...',
        'Start': '开始',
        'End': '结束',
        'List All': '全部列表',
        'Switch': '切换',
        'Merge': '合并',
        'Available Sessions': '可用会话',
        'Inactive': '未激活',
        'File path (optional)': '文件路径(可选)',
        'For file-specific operations': '用于针对单个文件的操作',
        'For commit operations': '用于提交操作',
        'Commit Message': '提交信息',
        'Branches': '分支',
        'Branch Tree': '分支树',
        'Loading branch tree...': '加载分支树...',
        'Log': '日志',
        'Diff': '差异',
        'Tree': '树',
        'Git output will appear here...': 'Git 输出将显示在这里...',
        'Modified Files': '修改的文件',

        // === Memory section ===
        'Store New Memory': '存储新记忆',
        'Store Memory': '存储记忆',
        'Search Memory': '搜索记忆',
        'Memory Browser': '记忆浏览器',
        'Memory Content': '记忆内容',
        'What do you want to remember?': '你想记住什么?',
        'Optional - auto-generated if empty': '可选 - 留空将自动生成',
        'Learning': '学习',
        'Progress': '进展',
        'Preference': '偏好',
        'Mistake': '错误',
        'Solution': '方案',
        'Integration': '集成',
        'Category': '分类',
        'Category Filter': '分类过滤',
        'Subcategory': '子分类',
        'Content': '内容',
        'Context': '上下文',
        'Tags': '标签',
        'Importance': '重要程度',
        'Importance:': '重要程度:',
        'Related Files': '相关文件',
        'Session': '会话',
        'Dedupe': '去重',
        'Collapse duplicate memories': '合并重复记忆',
        'Edit Memory': '编辑记忆',
        'No memories found': '未找到记忆',
        'Verified': '已验证',
        'Recent (7 days)': '最近(7 天)',

        // === Logs ===
        'Live': '实时',
        'Pause': '暂停',
        'Filter:': '过滤:',
        'Clear': '清空',
        'Clear Logs': '清空日志',
        'Clear All Local Data': '清空所有本地数据',
        'Clear History': '清空历史',
        'Clear history': '清空历史',
        'Clear Tool Call History': '清空工具调用历史',
        'Clear Results': '清空结果',
        'Complete log of all API interactions for analysis': '所有 API 交互的完整日志,供分析使用',

        // === Provider section ===
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

        // === Working directory ===
        'Active Directory Path': '活跃目录路径',
        'Change Directory': '切换目录',
        'Change & Reinitialize': '切换并重新初始化',
        'Changing the working directory will reinitialize all services including search index, git repo, and memory system.': '切换工作目录会重新初始化所有服务,包括搜索索引、Git 仓库和记忆系统。',
        'Path': '路径',
        'New Directory Path': '新目录路径',
        'Directory Path': '目录路径',
        'Directory Contents': '目录内容',
        'Directory Change History & Statistics': '目录变更历史与统计',
        'Recent Directories': '最近目录',
        'Set as Working Directory': '设为工作目录',
        'Use . for current, or enter relative path': '用 . 代表当前目录,或输入相对路径',
        'Enter absolute path to your project directory': '输入项目目录的绝对路径',
        'Click "Browse Directory" to view contents': '点击"浏览目录"查看内容',
        'Click "Reload" to render the graph...': '点击"重新加载"渲染图...',
        'Click a button above to view project details': '点击上方按钮查看项目详情',
        'Click to switch theme (light → dark → system)': '点击切换主题(浅色 → 深色 → 跟随系统)',
        'Drag nodes · scroll to zoom · click for details': '拖动节点 · 滚轮缩放 · 点击查看详情',
        'Switch language': '切换语言',
        'Exists': '存在',
        'Readable': '可读',
        'Writable': '可写',
        'Structure': '结构',
        'Dependencies': '依赖',

        // === Settings ===
        'General': '通用',
        'Appearance': '外观',
        'About': '关于',
        'Version': '版本',
        'License': '许可证',
        'Data Management': '数据管理',
        'Local data: Tool history, preferences, recent directories': '本地数据:工具历史、偏好、最近目录',
        'These actions cannot be undone': '此操作不可撤销',
        'Danger Zone': '危险区域',
        'Rebuild Index': '重建索引',

        // === Misc ===
        'Total:': '总计:',
        'Operation completed': '操作已完成',
        'Operation failed': '操作失败',
        'You are absolutely right': '你说得对',
        '0 logs': '0 条日志',
        '0ms': '0 毫秒',
        'View tool call history': '查看工具调用历史',
        'System logs will appear here...': '系统日志将显示在这里...',
        'hub (≥5 edges)': '中心节点(≥5 条边)',
        'node (callee)': '节点(被调用方)',
        '*.py, *.js, etc.': '*.py、*.js 等',
        '(whole project)': '(整个项目)',
        'path/to/file.py': '路径/到/文件.py',
        'path/to/existing_file.py': '路径/到/已存在的文件.py',
        'path/to/new_file.py': '路径/到/新文件.py',
        'C:/path/to/your/project': 'C:/路径/到/你的/项目',
    };

    // Reverse map for switching back to English
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

    // Translate one literal by exact match (whitespace-tolerant)
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
        if (!scope || !scope.querySelectorAll) return;
        var map = pickMap(toLang);

        var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };

        // 1. Walk all text nodes (including descendants of `scope`)
        try {
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
        } catch (e) { /* TreeWalker can throw on detached nodes — ignore */ }

        // 2. Translate placeholders / titles / aria-label / alt
        try {
            var els = scope.querySelectorAll(
                '[placeholder], [title], [aria-label], [alt], [data-tooltip]'
            );
            els.forEach(function (el) {
                ['placeholder', 'title', 'aria-label', 'alt', 'data-tooltip'].forEach(function (attr) {
                    var v = el.getAttribute(attr);
                    if (!v) return;
                    var t = translateText(v, map);
                    if (t != null) el.setAttribute(attr, t);
                });
            });
        } catch (e) { /* ignore */ }
    }

    // Keyed lookup for places that need theme.toggleHint etc.
    var KEYED = {
        en: {
            theme: { toggleHint: 'Click to switch theme (light → dark → system)',
                     light: 'Light', dark: 'Dark', system: 'System' },
            lang: { toggleHint: 'Switch language' },
        },
        zh: {
            theme: { toggleHint: '点击切换主题(浅色 → 深色 → 跟随系统)',
                     light: '浅色', dark: '深色', system: '跟随系统' },
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
        if (s && /^[a-z][a-zA-Z0-9_.]*$/.test(s)) {
            var keyed = tKeyed(s);
            if (keyed != null) return keyed;
        }
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
        // First update current so that observers/listeners that fire during
        // translation see the new state.
        var prev = current;
        current = lang;
        writeStored(lang);
        translateSubtree(root.document.body, lang);
        try {
            root.document.documentElement.setAttribute(
                'lang', lang === 'zh' ? 'zh-Hans' : 'en'
            );
        } catch (e) { /* ignore */ }
        refreshButtons();
        try {
            root.dispatchEvent(new CustomEvent('languagechange', {
                detail: { lang: lang, previous: prev }
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
        // Force a full retranslate of the whole page (used after a
        // section is loaded and we're already in zh mode).
        applyTranslations: function () {
            translateSubtree(root.document.body, current);
            refreshButtons();
        },
    };
    root.tr = t;

    function bootstrap() {
        if (current === 'zh') {
            translateSubtree(root.document.body, 'zh');
            try { root.document.documentElement.setAttribute('lang', 'zh-Hans'); }
            catch (e) { /* ignore */ }
        }
        refreshButtons();
    }

    if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }

    // Re-run on every dynamic section load — sectionLoaded is fired by
    // componentLoader after innerHTML + scripts.  We use rAF so any inline
    // script that injects more DOM has a chance to run first.
    root.document.addEventListener('sectionLoaded', function () {
        if (current === 'zh') {
            requestAnimationFrame(function () {
                translateSubtree(root.document.body, 'zh');
            });
        }
        refreshButtons();
    });

    // Also handle componentLoader's appended modals (they fire mutation
    // observer events but no sectionLoaded).
    var pending = [];
    var scheduled = false;
    function flushPending() {
        scheduled = false;
        if (current !== 'zh') { pending = []; return; }
        var batch = pending.slice();
        pending = [];
        batch.forEach(function (n) {
            if (n && n.isConnected) translateSubtree(n, 'zh');
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
    } catch (e) { /* old browsers — ignore */ }
})(typeof window !== 'undefined' ? window : this);
