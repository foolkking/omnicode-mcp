# OmniCode-MCP v2 架构重构规划

> **定位调整**：从"带 AI 编辑能力的 MCP server"转向"面向 AI Agent / AI 编辑器的本地优先 Codebase Intelligence Layer"。
>
> 核心价值：不是替代 Cursor / Claude Code / Kiro / Continue / Aider，而是为这些工具提供更高质量、更安全、更省 token 的代码库理解和操作接口。
>
> 最后更新：2026-05-24

---

## 一、项目定位

### 核心卖点

1. **让 AI 更准确地理解代码库** — AST + LSP + 调用图 + 继承图 + Git 历史
2. **让 AI 更少读取完整文件** — outline / symbols / relevant_chunks / diagnostics 多模式读取
3. **让 AI 修改前获得完整上下文** — 符号、引用、诊断、调用图、记忆、Git 历史、影响范围
4. **让代码修改可审查** — patch preview → validate → apply → rollback → edit session 记录
5. **本地优先、可控、可审查** — 全离线可用，API key 本地加密，不依赖任何云服务

### 不做什么

- ❌ 不做完整聊天式 AI 编辑器
- ❌ 不做 VS Code 替代品
- ❌ 不做自研 Agent 框架
- ❌ 不做多用户 SaaS（近期）

---

## 二、两种运行模式

### 模式 A：Console / Debug Mode

面向开发者和调试场景。

```bash
omnicode serve --console
# 或
omnicode dev
```

包含：
1. Web Console（Index 状态 / Search Debug / Edit Session / Diff Preview / Memory Viewer / Provider Debug / Call Graph / Logs / MCP Tool Inspector）
2. 所有 HTTP API
3. WebSocket 实时日志
4. MCP stdio（可选）

### 模式 B：Headless / API Mode

面向 Cursor / Claude Code / Kiro / Continue / Aider 等外部 AI 编辑器。

```bash
omnicode mcp
# 或
omnicode serve --headless
```

包含：
1. MCP stdio
2. HTTP API（可选）
3. 核心索引 / 搜索 / 文件读取 / 符号定位 / 引用解析 / 诊断 / patch 操作

**不包含** Web UI。

---

## 三、目标架构

```
omnicode_core/                    ← 核心层（不依赖 Web UI，不依赖 LLM）
├── index/                        ← 增量索引引擎
│   ├── file_tracker.py           ← mtime + hash 变更检测
│   ├── chunker.py                ← AST-aware 分块
│   └── embedding.py              ← 向量化（可配置模型）
├── search/                       ← 混合召回 + 重排序
│   ├── vector_search.py          ← FAISS 语义搜索
│   ├── symbol_search.py          ← 符号名模糊匹配
│   ├── text_search.py            ← 子串扫描
│   ├── git_search.py             ← Git 历史搜索
│   ├── reranker.py               ← 多路召回重排序
│   └── why_matched.py            ← 匹配原因解释
├── ast/                          ← Tree-sitter 结构解析
│   ├── parser.py                 ← 7 语言 AST
│   ├── symbols.py                ← 符号提取
│   └── outline.py                ← 文件骨架生成
├── lsp/                          ← LSP 桥接
│   ├── bridge.py                 ← multilspy 代理
│   ├── definition.py             ← goto_definition
│   ├── references.py             ← find_references
│   ├── hover.py                  ← hover / type info
│   ├── diagnostics.py            ← get_diagnostics
│   └── rename.py                 ← rename_symbol
├── graph/                        ← 调用图 + 影响分析
│   ├── call_graph.py             ← 调用关系构建
│   ├── inheritance.py            ← 继承关系
│   ├── impact.py                 ← 影响半径分析
│   ├── dead_code.py              ← 死代码检测
│   └── entrypoints.py            ← 入口点发现
├── memory/                       ← 记忆系统
│   ├── store.py                  ← 存储 + 去重
│   ├── search.py                 ← 混合搜索
│   ├── advisory.py               ← 主动召回
│   └── types.py                  ← 记忆类型定义
├── edit/                         ← 安全编辑（不依赖 LLM）
│   ├── patch.py                  ← preview / validate / apply / rollback
│   ├── session.py                ← edit session 管理
│   ├── snapshot.py               ← 文件快照
│   └── explain.py                ← patch 解释
├── diagnostics/                  ← 静态分析门禁
│   ├── guard.py                  ← ruff / eslint / cppcheck
│   └── reporter.py              ← 结构化报告
├── read/                         ← 多模式文件读取
│   ├── full.py                   ← 完整文件
│   ├── outline.py                ← 只返回签名
│   ├── symbols.py                ← 只返回符号列表
│   ├── relevant_chunks.py        ← 相关代码块
│   ├── diagnostics.py            ← 只返回诊断
│   ├── imports.py                ← 只返回导入
│   └── tests.py                  ← 只返回相关测试
└── config/                       ← 配置管理
    ├── settings.py
    └── features.py               ← 功能开关

omnicode_adapters/                ← 适配层（调用 core）
├── mcp_server/                   ← MCP stdio 适配
│   ├── tools.py                  ← 高层聚合工具（omni_search / omni_read / omni_edit / ...）
│   └── resources.py              ← MCP resources
├── http_api/                     ← FastAPI REST 适配
│   ├── routers/
│   └── app.py
├── web_console/                  ← Web UI 适配
│   ├── templates/
│   ├── static/
│   └── sections/
└── cli/                          ← 命令行入口
    ├── main.py                   ← omnicode init / index / status / mcp / serve / dev / doctor
    └── commands/

omnicode_llm/                     ← LLM 增强层（可选，pip install omnicode-mcp[llm]）
├── router/                       ← 多模型路由
│   ├── litellm_provider.py
│   ├── provider_registry.py
│   └── best_of_n.py
├── edit_agent/                   ← AI 生成 patch
│   ├── generate.py
│   ├── review.py
│   └── repair.py
└── providers/                    ← Provider 管理
    ├── secret_box.py
    └── selection.py
```

### 核心原则

1. `omnicode_core` **不依赖** Web UI
2. `omnicode_core` **不依赖** 具体 LLM provider
3. MCP / HTTP / Web Console 都调用同一套 core service
4. LLM 相关能力是 **optional enhancement**，不是核心依赖

---

## 四、MCP Tools 瘦身方案

### 当前：25+ tools，~10k tokens

### 目标：6 个高层工具 + 1 个 discovery，~3k tokens

```
omni_search(query, mode="auto")
    → 内部决定调用 symbol / text / semantic / git / memory / lsp 哪些搜索器

omni_read(file, mode="outline|symbols|full|relevant_chunks|diagnostics|imports|tests")
    → 多模式读取，默认 outline

omni_edit(action="preview|validate|apply|rollback|explain", patch=..., session_id=...)
    → 安全编辑全流程

omni_analyze(symbol, analysis="impact|callers|callees|entrypoints|dead_code|related_tests")
    → 影响分析

omni_memory(action="search|store|advisory|context", ...)
    → 记忆操作

omni_context(file, symbol, task)
    → 一次性返回：outline + callers + callees + references + tests + git + memory + diagnostics

discover_tools(query)
    → 按需返回完整 schema
```

### 向后兼容

旧的 25 个细粒度 tool 仍然注册，但标记为 `deprecated`。新客户端用 6 个高层工具。

---

## 五、结构化省 token 接口

### read_file 多模式

| mode | 返回内容 | 典型 token |
|---|---|---|
| `full` | 完整文件 | ~3000 (1000 行) |
| `outline` | 签名 + docstring 第一行 | ~150 |
| `symbols` | 符号列表 (name/kind/lines) | ~200 |
| `relevant_chunks` | 与 query 相关的代码块 | ~500 |
| `diagnostics` | ruff/eslint 诊断 | ~100 |
| `imports` | import 语句 | ~50 |
| `tests` | 相关测试文件路径 | ~30 |

### get_related_context(file, symbol, task)

一次调用返回：
1. 当前 symbol outline
2. 调用者（callers）
3. 被调用者（callees）
4. 引用位置（references）
5. 相关测试文件
6. 最近 Git 修改
7. 相关 memory
8. diagnostics
9. 可能影响范围
10. 推荐检查命令

### analyze_impact(symbol)

```json
{
  "changed_symbol": "ProviderRegistry.test_provider",
  "direct_callers": 4,
  "related_tests": 3,
  "risk": "medium",
  "suggested_checks": [
    "pytest tests/test_provider_registry.py",
    "pytest tests/test_api_provider_crud.py"
  ]
}
```

---

## 六、LSP-MCP 桥接

### 优先支持

| 语言 | LSP Server | 安装方式 |
|---|---|---|
| Python | pyright | `pip install pyright` |
| TypeScript/JS | tsserver | `npm i -g typescript` |

### 后续扩展

| 语言 | LSP Server |
|---|---|
| Go | gopls |
| Rust | rust-analyzer |
| C/C++ | clangd |

### 暴露能力

```
goto_definition(file, line, col)
find_references(file, line, col)
hover(file, line, col)
document_symbols(file)
workspace_symbols(query)
get_diagnostics(file?)
rename_symbol(file, line, col, new_name)
```

### 与 tree-sitter 互补

| 用途 | tree-sitter | LSP |
|---|---|---|
| 快速结构解析 | ✅ | |
| outline / chunking | ✅ | |
| 调用图初步构建 | ✅ | |
| 语言无关基础索引 | ✅ | |
| 定义跳转 | | ✅ |
| 引用查找 | | ✅ |
| hover / type 信息 | | ✅ |
| diagnostics | | ✅ |
| rename | | ✅ |
| 跨文件精确关系 | | ✅ |

---

## 七、增量索引

### 文件追踪表

```sql
CREATE TABLE file_index (
    file_path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    content_hash TEXT,
    language TEXT,
    last_indexed_at TEXT,
    symbol_hash TEXT,
    embedding_hash TEXT
);
```

### Rebuild 分类

| 变化类型 | 操作 |
|---|---|
| unchanged | 跳过 |
| 只改注释/空白 | 更新文本索引 |
| 函数体改动 | 更新 chunk embedding |
| 函数签名改动 | 更新 symbol index + call graph |
| import 改动 | 更新 dependency graph |
| 文件删除 | 删除 symbols / chunks / edges |
| 新文件 | 新增索引 |

### 目标

小型项目 rebuild：30-60s → 2-3s

---

## 八、搜索系统升级

### 混合召回 + 重排序 Pipeline

```
用户 query
    ↓
Query Intent Classifier
    ↓
并行召回：
  1. Symbol Search
  2. Text Search
  3. Vector Search
  4. Git History Search
  5. LSP Workspace Symbols
  6. Memory Search
    ↓
Reranker
    ↓
结构化结果（含 why_matched）
```

### 结果格式

```json
{
  "file": "...",
  "symbol": "...",
  "kind": "function/class/method",
  "score": 0.87,
  "why_matched": ["symbol_name", "embedding", "recent_git_change"],
  "snippet": "...",
  "definitions": [...],
  "references_count": 12,
  "diagnostics": [...]
}
```

### Embedding 模型可配置

| 场景 | 模型 |
|---|---|
| default | all-MiniLM / bge-small |
| code-specific | unixcoder / codebert / starcoder embedding |
| remote (optional) | OpenAI / Voyage / Jina |

---

## 九、编辑系统 → Patch Session

### Pipeline

```
Plan → Context Collect → Patch Generate → Patch Preview
  → Static Check → Auto Review → Auto Repair → Human Review
  → Apply → Rollback
```

### 核心接口（不依赖 LLM）

```
preview_patch(patch)
validate_patch(patch)
apply_patch(patch, session_id)
rollback_patch(session_id)
explain_patch(patch)
list_edit_sessions()
read_edit_session(session_id)
```

### LLM 增强接口（可选）

```
generate_patch_with_llm(instructions, context)
review_patch_with_llm(patch)
repair_patch_with_llm(patch, errors)
```

### Edit Session 记录

```json
{
  "session_id": "...",
  "model": "gemini-2.5-flash",
  "prompt": "...",
  "files_changed": ["router.py"],
  "diff": "...",
  "checks_before": {...},
  "checks_after": {...},
  "applied_at": "...",
  "rollback_available": true
}
```

### 存储

```
.data/snapshots/          ← 文件快照（apply 前备份）
.data/edit_sessions/      ← session JSON 记录
```

---

## 十、Memory 主动召回

### 触发条件

当用户或外部 AI 请求修改代码时，根据以下内容自动召回：
1. 当前文件路径
2. 当前 symbol
3. Git diff
4. 报错信息
5. 测试失败信息
6. 用户任务描述
7. 相关依赖名
8. 历史 edit session

### Advisory 格式（300-800 tokens）

```
Relevant past lessons:
1. 上次修改 providers registry 时，遗漏了 encrypted key migration。
2. edit pipeline 中不能直接 trust model output，要先 strip thinking blocks。
3. FastAPI provider test endpoint 曾因字段名不一致失败。
```

### Memory 类型

```
bug_lesson
architecture_decision
user_preference
project_convention
failed_attempt
successful_pattern
api_contract
```

### 召回元数据

```json
{
  "why_recalled": "file_path match + symbol overlap",
  "match_field": "related_files",
  "confidence": 0.82,
  "related_files": ["omnicode/llm/router.py"]
}
```

---

## 十一、调用图 → 影响分析

### 面向 AI 的图分析接口

```
get_callers(symbol, depth=1)
get_callees(symbol, depth=1)
get_impact_radius(symbol, depth=2)
explain_dependency_path(source, target)
find_entrypoints(file_or_symbol)
find_dead_symbols()
find_high_risk_symbols()
suggest_related_tests(symbol)
```

### 目标

让 AI 在修改函数前知道：
1. 谁调用了它
2. 它调用了谁
3. 修改会影响哪些文件
4. 哪些测试应该运行
5. 风险等级是什么

可视化只是附加价值，**真正核心是 impact analysis**。

---

## 十二、Web UI 定位

### 是什么

"AI 代码操作的可视化审查与调试台"

### 聚焦

1. Index 状态
2. Search Debug
3. Edit Session / Diff Preview
4. Memory Viewer
5. Provider Debug
6. Call Graph / Impact Graph
7. Logs
8. MCP Tool Inspector

### 不做

- ❌ 完整聊天式 AI 编程界面
- ❌ 复杂 IDE 替代功能
- ❌ VS Code 扩展（近期）

---

## 十三、部署模式

### 三种模式

| 模式 | 代码位置 | 索引位置 | 编辑位置 |
|---|---|---|---|
| local | 本地 | 本地 | 本地 |
| cloud | 云端副本 | 云端 | 云端 |
| hybrid | 本地 | 云端 | 本地 apply |

### 推荐：hybrid mode

```
本地：
  - 保存真实代码
  - 文件监听
  - Git diff
  - 安全编辑 apply

云端：
  - embedding
  - 搜索索引
  - memory
  - 调用图分析
  - Web Console
```

### 安全边界

1. API Key 鉴权
2. Workspace 隔离
3. 文件访问沙箱
4. 禁止路径穿越
5. Provider API key 加密
6. Master key 从环境变量读取
7. 日志中永不打印 secret
8. Edit 权限分级
9. HTTPS 反代支持

### 权限分级

```
read_only        ← 只能搜索、读取
suggest_patch    ← 可以生成 patch 但不能 apply
apply_patch      ← 可以应用 patch
admin            ← 完全控制
```

### 配置示例

```toml
[server]
mode = "cloud"
host = "0.0.0.0"
port = 8765
auth = true

[workspace]
root = "/srv/omnicode/workspaces/project-a"
read_only = false

[features]
web_console = true
mcp_http = true
llm_router = false
lsp = true
memory = true
safe_edit = true

[index]
incremental = true
embedding_device = "cpu"
embedding_model = "bge-small-en"

[security]
require_api_key = true
allow_apply_patch = false
allow_shell = false
```

---

## 十四、云端资源估计

| 规模 | 文件数 | 代码行数 | 推荐配置 |
|---|---|---|---|
| 小型 | 100-500 | 1万-5万 | 1-2 vCPU, 2-4 GB RAM |
| 中型 | 500-3000 | 5万-30万 | 2-4 vCPU, 4-8 GB RAM |
| 大型 | 3000-20000 | 30万-200万 | 4-8 vCPU, 16 GB RAM |
| 多项目/多用户 | — | — | 8 vCPU, 16-32 GB RAM |

个人云端推荐：
- 最低可用：2 vCPU, 4 GB RAM, 40 GB SSD
- 较舒适：4 vCPU, 8 GB RAM, 80-100 GB SSD

默认不需要 GPU。

---

## 十五、工程化补强

### CLI 命令

```bash
omnicode init          # 初始化 .data/ 目录
omnicode index         # 增量索引
omnicode status        # 显示索引状态
omnicode mcp           # 启动 MCP stdio
omnicode serve --headless   # 只启动 API
omnicode serve --console    # 启动 API + Web UI
omnicode dev           # 开发模式（console + reload）
omnicode doctor        # 检查环境（Python / LSP / 模型 / 端口）
```

### Docker

```yaml
# docker-compose.yml
services:
  omnicode:
    build: .
    ports:
      - "6789:6789"
    volumes:
      - ./:/workspace
      - omnicode-data:/workspace/.data
    environment:
      - TRANSFORMERS_OFFLINE=1
volumes:
  omnicode-data:
```

### GitHub Actions

```yaml
# .github/workflows/ci.yml
- pytest tests -q
- ruff check omnicode api core tests
- docker build .
```

---

## 十六、优先级

### P0（必须先做）

1. Core / Adapter / Optional LLM 架构解耦
2. Headless mode 与 Console mode 拆分
3. LSP-MCP 桥接（Python + TypeScript）
4. 增量索引
5. Patch Preview / Validate / Apply / Rollback
6. MCP tools 瘦身（6 高层工具）
7. 结构化省 token 接口（read mode=outline）

### P1（紧随其后）

1. Search rerank + why_matched
2. Auto Memory Advisory
3. Impact Analysis
4. Edit Session 页面
5. Search Debug 页面
6. API key 鉴权
7. Docker Compose
8. GitHub Actions

### P2（中期）

1. Cloud mode
2. Hybrid mode
3. MCP-over-HTTP
4. 多 workspace
5. 权限系统
6. WebGL 大图渲染
7. 更多 embedding 模型
8. 多用户隔离

### 暂缓

1. 完整聊天式 AI 编辑器
2. 自研 Agent 框架
3. 过多 provider 扩展
4. VS Code 插件
5. 复杂多用户 SaaS

---

## 十七、最终目标

> **"本地优先、可云端部署、可被任意 AI 编辑器调用的 Codebase Intelligence Layer"**

核心能力：
1. 代码理解
2. 结构化上下文压缩
3. 搜索与引用解析
4. 调用图影响分析
5. 安全 patch 操作
6. 记忆主动召回
7. 可视化调试控制台
8. 可选 LLM 增强

**不是**"又一个 AI 代码编辑器"。
**而是**"让所有 AI 代码编辑器更懂代码库、更省 token、更安全改代码的底层服务"。

> **状态:** 1.0.0-rc1 已实现全部 8 项能力,且通过 `IntelligenceComposer` 单点编排:
>
> * REST: `POST /intelligence/context` — 单次调用拿到结构化、token 预算内的多能力聚合上下文
> * MCP: `omni_intelligence` 高级工具,内部代理同一组合器
> * 部署指纹: `GET /capabilities` 声明哪些能力在线、用了什么后端
> * 任何单一能力失败都被记入 `errors[<capability>]`,不影响其它能力
>
> 实现:`omnicode_core/intelligence/composer.py`,集成测试:`tests/integration/test_intelligence_endpoint.py`。

---

## 变更日志

- 2026-05-24:创建 v2 架构规划文档
- 2026-05-27:**P0 + P1 + P2 完成** — Intelligence Layer 已组装,版本 1.0.0-rc1。所有八项核心能力既可单独通过专项路由访问,也可通过 `IntelligenceComposer` 单次调用按 token 预算编排聚合输出。
