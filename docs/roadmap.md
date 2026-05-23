# OmniCode-MCP 路线图与改进方向

> 本文档记录基于 2025-2026 业界 MCP 最佳实践的可落地改进方向。
> 每项条目按 **价值 / 实现成本 / 优先级** 三轴评估，便于挑选下一阶段任务。
>
> 最后更新:2026-05-23

## 调研来源

- [Anthropic — building more efficient AI agents (Code Execution with MCP)](https://anthropic.com/engineering/code-execution-with-mcp), 2025-11
- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09
- [Anthropic — Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use), 2025-11
- [Claude Code v2.0.74 LSP integration](https://how2shout.com/news/claude-code-v2-0-74-lsp-language-server-protocol-update.html), 2025-12
- [skywork.ai — lsp-mcp bridging MCP and LSP](https://skywork.ai/blog/lsp-mcp-mcp-lsp-bridge/), 2025
- [stackone — MCP Token Optimization: 4 Approaches](https://www.stackone.com/blog/mcp-token-optimization/), 2025-12
- [mindstudio — Reduce token usage with MCP optimisation](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization), 2025-12
- [medium @mukundkidambi — Beyond GraphQL, what reduces token spend in MCP](https://medium.com/@mukundkidambi/beyond-graphql-what-actually-reduces-token-spend-in-mcp-servers-9aa3350e8d4d), 2025-12
- [Atlassian Labs — mcp-compressor](https://github.com/atlassian-labs/mcp-compressor), 2025
- [intuitionlabs — Claude Skills vs MCP comparison](https://intuitionlabs.ai/articles/claude-skills-vs-mcp), 2025-10
- [jonrad/lsp-mcp](https://github.com/jonrad/lsp-mcp), 2025
- [ceaksan — local semantic code search MCP](https://ceaksan.com/en/local-semantic-code-search-ai-mcp), 2025

> 内容已根据本项目情况重新组织,引用要点已转述以遵守版权要求。

---

## 一、高优先级方向(立竿见影)

### 1.1 LSP-MCP 桥接 ⭐⭐⭐⭐⭐

**做什么**:暴露 LSP 五大核心操作给 LLM:
- `goToDefinition` — 跳转符号定义
- `findReferences` — 找全部引用
- `hover` — 类型 + 文档
- `documentSymbol` — 文件结构
- `getDiagnostics` — 编译错误/警告

**为什么有用**:
- 我们的 tree-sitter AST 只能定位声明,做不了真正的跨文件 reference 解析
  (例如 `import foo` 后 `foo.bar()` 是哪个 `bar`)
- LSP 是编译器精度;社区数据显示相比文本搜索 false negative 率从 ~50% 降到 ~0%
- Claude Code v2.0.74(2025-12)已经原生支持,意味着这是 2026 的 baseline 能力

**实现路径**:
- Python 层用 `multilspy` 库,作为 LSP client 启动 pyright / typescript-language-server / gopls / rust-analyzer / clangd
- 在 `mcp_server.py` 加 5 个 tool;在 FastAPI 加对应 REST endpoint
- DB 缓存 LSP 响应(避免每次 startup language server 冷启动)

**成本**:1-2 天
**优先级**:⭐⭐⭐⭐⭐

---

### 1.2 Code Execution Tool ⭐⭐⭐⭐⭐

**做什么**:Claude 不再"call A → 看结果 → call B",而是写一段 Python 在 sandbox 里直接做 orchestration。

**为什么有用**:
- Anthropic 引用的实测案例:典型多步任务 ~150k tokens 降到 ~25k
- 准确率反而提升,因为 LLM 不需要在 stop token 之间重组中间结果

**实现路径**:
- Sandbox:进程隔离 + 资源 cgroup + 白名单 import + 强制超时
- 把现有 `apiRoutes.*` 等价 binding 暴露成 `omnicode.*` Python 模块
- 在 MCP 层加 `execute_code(code: str)` tool

**成本**:3-5 天(sandbox 安全是难点)
**优先级**:⭐⭐⭐⭐⭐

---

### 1.3 Symbol Outline 模式 ⭐⭐⭐⭐

**做什么**:`/read?mode=outline` 时,只返回文件的"骨架":类名 + 函数签名 + 顶部 docstring 一行。
LLM 看完 outline 再请求具体函数体。

**为什么有用**:
- 1000 行文件直接全读 ≈ 3000 tokens;outline ≈ 150 tokens
- 我们已经有 `list_symbols_in_file` 后端,只是没暴露给 read

**实现路径**:
- `api/v1/routers/files.py` 的 `/read` 加 `mode: str = "full" | "outline" | "symbol"`
- outline 模式:走 chunker.extract_symbols + 取每个的 first significant line(签名)+ docstring 第 1 行
- 客户端(graph-viewer / search.html)在文件超 200 行时默认用 outline

**成本**:1 天
**优先级**:⭐⭐⭐⭐

---

### 1.4 Diagnostics-First Search ⭐⭐⭐⭐

**做什么**:语义/符号搜索结果里附带"该文件的 ruff/lint 已知问题"。LLM 一次拿到代码 + 已知问题,修 bug 命中率提升。

**为什么有用**:
- 我们已有 `ProactiveGuard` 模块跑 ruff/eslint/cppcheck
- 把它接到 search 结果上几乎零成本

**实现路径**:
- `LegacySearchResult` 加 `diagnostics: List[dict]` 字段
- 搜索时按结果 file_path 调 `guard.check(file_path)`,只取该范围内的诊断
- UI 在结果卡下方加红色/黄色标记

**成本**:半天
**优先级**:⭐⭐⭐⭐

---

### 1.5 Tool Description 压缩 ⭐⭐⭐⭐

**做什么**:MCP server 给 Claude 列工具时,剥掉 description / enum / nested type 文档,只保留 parameter shape。

**为什么有用**:
- 我们 25+ 个 tool 的 schema 估 ~10k tokens
- 实测剥离后能省一半,Claude 仍能正确调用
- Atlassian 开源的 mcp-compressor 提供了 reference impl

**实现路径**:
- 在 `mcp_server.py` 加一层 wrapper,启动时检测 `MCP_COMPRESS=1` 环境变量
- 每个 tool 描述只保留:名字 + 第一句 + 参数名/类型(无 description)

**成本**:1 天
**优先级**:⭐⭐⭐⭐

---

## 二、中优先级方向(明显收益)

### 2.1 TOON 编码(替代 JSON 输出)⭐⭐⭐

**做什么**:对大型 tool 输出(如 call graph 6052 边)用 TOON 格式输出,而不是 JSON。

**为什么有用**:
- MindStudio 实测 30-60% token 节省,模型解析率不降
- 我们 `/search/symbols/graph` 输出 JSON 巨大

**实现路径**:
- 加 `?format=toon` query param;serializer 单独写
- 因为客户端是浏览器,只对 MCP 接入做 TOON,Web UI 仍 JSON

**成本**:半天
**优先级**:⭐⭐⭐

---

### 2.2 Skills Framework ⭐⭐⭐

**做什么**:把"修 bug"、"加测试"、"refactor"打包成 `SKILL.md`(步骤 + 资源 + tool 引用)。Claude 自动发现并使用。

**为什么有用**:
- Anthropic 引用的极端案例:99.6% token reduction
- 避免每次重新发 system prompt

**实现路径**:
- 建 `skills/` 目录,每个 skill 一个文件夹
- 在 MCP 加 `list_skills` / `invoke_skill(name, args)` 两个 tool

**成本**:2-3 天
**优先级**:⭐⭐⭐

---

### 2.3 Embedding 缓存 + Incremental Reindex ⭐⭐⭐

**做什么**:`POST /search/index` 不再整库重 embed。基于 file mtime + sha256 增量。

**为什么有用**:
- 仓库越大,全量 reindex 越慢;我们当前 ~125 文件已经要 30s+
- 如果换成在线 embedding 模型(替代 sentence-transformers),省钱

**实现路径**:
- `chunks` 表加 `content_hash TEXT`(已有 mtime 间接信息)
- index 流程先比 hash,改了才 re-embed
- 提供 `/search/index?force=true` 强制全量

**成本**:1 天
**优先级**:⭐⭐⭐

---

### 2.4 Memory → Auto-context Injection ⭐⭐⭐

**做什么**:根据当前文件 / git diff / 最近搜索自动召回相关 memory,无需用户手动 search。

**为什么有用**:
- 现在 memory 是被动的:LLM 不主动看就漏掉
- 主动注入可以让"上次踩坑笔记"自动出现在 edit pipeline 的 context 里

**实现路径**:
- 已有 `_collect_memory_advisory`,扩展成"按 file_path / 关键 symbol 自动召回"
- 加 file fingerprint → memory 索引

**成本**:2 天
**优先级**:⭐⭐⭐

---

### 2.5 Tool Search(代替 list-all)⭐⭐⭐

**做什么**:MCP 启动时不发全部 25 工具描述,只发一个 `search_tools(query)`。LLM 按需发现。

**为什么有用**:
- 启动 token 从 ~10k 降到几百
- 工具越多,这个杠杆越大

**实现路径**:
- 注册一个 meta-tool `discover_tool(query: str)`
- 内部用我们的语义搜索引擎给 tool 描述建 embedding 索引

**成本**:1 天
**优先级**:⭐⭐⭐

---

## 三、低优先级方向(细节体验)

### 3.1 highlight.js 语法高亮 ⭐⭐

**做什么**:View Code modal 接 highlight.js,代码按语言高亮。

**当前状态**:已实现(本次任务)

---

### 3.2 WebGL Canvas 大图渲染 ⭐⭐

**做什么**:节点数 >1500 时切到 WebGL(deck.gl 或 sigma.js),SVG 卡顿。

**当前状态**:已实现(本次任务,2000+ 节点自动切 canvas)

---

### 3.3 "Open in Editor" 协议跳转 ⭐⭐

**做什么**:View Code modal / graph viewer 加按钮,通过 `vscode://file/...` 协议跳转到本地编辑器。

**当前状态**:已实现(本次任务)

---

### 3.4 Provider Test 失败时被拒字段高亮 ⭐⭐

**做什么**:Test 失败时根据 hint 字段类型(api_key / api_base / model),给对应输入框加红框。

**当前状态**:已实现(本次任务)

---

### 3.5 ruff/JS 检查后 auto-review-pass ⭐⭐

**做什么**:edit pipeline 跑完后调 Guard 检查,有 ERROR 时自动让 LLM 修一次。

**实现路径**:
- 现有 `EditResult` 加 `review_passes: int` 字段
- Guard 返回 errors → 重新喂给 edit pipeline 一次,instructions 用 errors 拼

**成本**:1 天
**优先级**:⭐⭐

---

## 四、对照本项目当前缺口

| 路线项 | 业界做了 | 我们做了 | 我们差距 |
|---|:---:|:---:|---|
| LSP 集成 | ✅(Claude Code 2.0.74) | ❌ | 完全没有 |
| Code Execution | ✅(Anthropic native) | ❌ | 完全没有 |
| Symbol Outline | 部分项目 | 后端 OK,read 端点缺 mode 参数 | API surface 不完整 |
| 工具描述压缩 | ✅(Atlassian 等) | ❌ | 完全没有 |
| TOON 输出 | 部分项目 | ❌ | 完全没有 |
| Skills Framework | ✅(Anthropic) | ❌ | 完全没有 |
| 增量索引 | 部分 | ❌ | 全量重建 |
| LSP-grade 跨文件引用 | LSP-MCP | ❌(只有 AST 声明) | 缺 |
| Diagnostics 注入搜索 | Claude Code | 有 Guard 模块未串联 | 接线 |
| 暗色 / i18n / 集群着色 | — | ✅ | — |
| MCP 工具数量 | 通常 5-15 | 25+ | 我们偏多 |
| 调用图可视化 | 少见 | ✅ D3 + 集群 + 路径过滤 | 我们领先 |
| 端到端 edit pipeline | 罕见 | ✅ 三层防御 | 我们领先 |

---

## 五、推荐落地顺序

如果资源有限,建议优先级:

1. **LSP-MCP 桥接** — 1-2 天,补我们做不到的事(跨文件引用)
2. **Symbol Outline 模式** — 1 天,token 减半立竿见影
3. **Diagnostics-First Search** — 半天,Guard 模块已有,只差接线
4. **Tool Description 压缩** — 1 天,MCP 启动 token 减半
5. **Code Execution Tool** — 3-5 天,长期 ROI 最大但 sandbox 难

完成 1-4 后,我们的 token 效率应能提升 ~50%,意图捕获精度因 LSP 加入会显著提升。

---

## 六、变更日志

- 2026-05-23: 创建文档,基于第一轮调研整理 10 个方向
- 待续
