<div align="center">

# OmniCode-MCP

**给 AI 编辑器接上"读懂仓库"的能力。**

让 Cursor、Claude、Continue、Aider、Kiro 以及任何 MCP 兼容客户端在同一个端点
里 *理解* 你的代码库 — 检索、影响分析、安全打补丁、记忆召回 — 一律支持
MCP、REST 与 WebSocket。

[![Python](https://img.shields.io/badge/python-3.11%20|%203.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7C3AED)](https://modelcontextprotocol.io/)
[![Tree-sitter](https://img.shields.io/badge/tree--sitter-7%20种语言-22C55E)](https://tree-sitter.github.io/tree-sitter/)
[![LSP](https://img.shields.io/badge/LSP-10%20种语言-0EA5E9)](https://microsoft.github.io/language-server-protocol/)
[![Tests](https://img.shields.io/badge/tests-433%20通过-brightgreen)](#测试)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0--rc1-orange)](pyproject.toml)

[**English**](README.md) · [**简体中文**](README_zh.md)

[快速开始](#-快速开始) ·
[工作原理](#-工作原理) ·
[MCP 工具](#-mcp-工具) ·
[使用场景](#-使用场景) ·
[文档](#-文档) ·
[常见问题](#-常见问题)

</div>

---

## ✨ 为什么是 OmniCode-MCP

LLM 在不了解你仓库的时候,会自信满满地胡说八道。多数编辑器扩展只把当前文件
和几条 grep 喂给模型,然后默默地把补丁打错。OmniCode-MCP 用 **八个可组合
能力** 补上这个缺口 — 这八件事是每个 AI 编辑器都需要、却很少同时具备的:

| # | 能力 | 做什么 | 模块 |
|---|---|---|---|
| 1 | **代码理解** | 7 种语言的 Tree-sitter AST,多种读取模式 (outline / symbols / full / range) | [`omnicode/ast_engine/`](omnicode/ast_engine/) |
| 2 | **上下文压缩** | 剥离注释、折叠函数体、按优先级裁剪 — 同样的 prompt 装下更多代码 | [`omnicode/llm/token_manager.py`](omnicode/llm/token_manager.py) |
| 3 | **混合搜索** | 语义 + 符号 + 文本 RRF 融合(短查询自动选 hybrid,也可显式 `mode=hybrid`),每条结果带 `why_matched`,让模型看到命中"原因" | [`omnicode/search/`](omnicode/search/) |
| 4 | **影响分析** | BFS 影响半径、调用方、被调方、风险评分、推荐测试 | [`omnicode_core/graph/impact.py`](omnicode_core/graph/impact.py) |
| 5 | **安全打补丁** | `preview` → `validate` → `apply` → `rollback`,LLM 不直接落盘 | [`omnicode_core/edit/patch.py`](omnicode_core/edit/patch.py) |
| 6 | **记忆召回** | 用户主动存入的项目记忆,编辑前按多角度(file/symbol/task/error/dependency)并行召回 | [`omnicode_core/memory/advisory.py`](omnicode_core/memory/advisory.py) |
| 7 | **调试控制台** | Web UI 看索引健康、diff 检视、advisory 抽屉、编辑会话浏览 | [`templates/`](templates/) |
| 8 | **可选 LLM** | 多 Provider 路由,熔断器、回退、Best-of-N — 通过 `[llm]` extras 启用 | [`omnicode/llm/router.py`](omnicode/llm/router.py) |

**一次 REST 调用** `POST /intelligence/context` 并行跑完八件事,在 token 预算
内拼好结构化 payload 返回。或者通过 **MCP 工具 `omni_intelligence`** 在
stdio / SSE / streamable-http 上拿到同样的结构。

> [!NOTE]
> OmniCode-MCP **不是** 又一个 AI 编辑器。它是 Cursor、Continue、Claude
> Code、Aider、Kiro 调用的服务。它们写代码,我们让它们写得更准、token 用
> 得更省。

---

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/foolkking/omnicode-mcp.git
cd omnicode-mcp

python -m venv .venv
. .venv/Scripts/activate          # Windows PowerShell
# . .venv/bin/activate            # macOS / Linux

pip install -e .                  # 仅核心(不含 LLM)
# pip install -e ".[llm]"         # 加多 Provider LLM 路由器
# pip install -e ".[agent]"       # 加文件监听器(用于 hybrid 模式)
# pip install -e ".[dev]"         # 加测试 + lint 工具
```

Conda 用户把 venv 那两行换成
`conda create -n omnicode-env python=3.11 -y && conda activate omnicode-env`。

> [!TIP]
> 不带 `[llm]` extra 的核心安装就提供 search / impact / patch / memory / MCP / Web 控制台。
> 设置 `OMNICODE_LLM_ROUTER=false` 让启动路径完全跳过 LLM 栈 — AI 编辑器
> 自带模型时这是最干净的部署形态。

### 运行

```bash
omnicode serve --console          # API + Web 控制台,在 http://127.0.0.1:6789/
omnicode serve --headless         # 仅 API,无 UI
omnicode mcp                      # MCP stdio (给 Claude / Cursor / Kiro 用)
omnicode dev                      # 控制台 + 自动 reload
```

首次建索引 30 – 60 秒,后续重建是增量的(2 – 3 秒)。

### 接入 Claude / Cursor / Kiro (MCP stdio)

把下面这段加到你客户端的 MCP 配置 — Claude Desktop 是
`claude_desktop_config.json`,Kiro 是 `~/.kiro/settings/mcp.json`,其他 MCP
兼容客户端类似:

```json
{
  "mcpServers": {
    "omnicode": {
      "command": "omnicode",
      "args": ["mcp"],
      "env": {
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1"
      }
    }
  }
}
```

重启客户端。默认注册 8 个高层工具。需要旧版的 16 个细粒度工具,设置
`OMNICODE_MCP_TOOLS=all`。

### 一行 composer 调用

最快感受到差别的方式 — 让 composer 一次调用拼好关于一个符号的 *所有*
信息。

```bash
curl -X POST http://127.0.0.1:6789/intelligence/context \
  -H 'content-type: application/json' \
  -d '{
        "task": "explain create_app",
        "file_path": "main.py",
        "symbol": "create_app",
        "token_budget": 4096
      }'
```

返回结构化 payload:文件大纲、调用图、影响、相关测试、近期 git 历史、匹配的
记忆建议 — 全部按 token 预算裁剪到位。

---

## 🧩 工作原理

```text
              ┌─────────────────────────────────────────────────────────┐
              │   AI 编辑器 / Agent  (Cursor · Claude Desktop · Kiro    │
              │   · Continue · Aider · 自定义 REST 客户端 · VS Code)    │
              └─────────────────────────────────────────────────────────┘
                            │ MCP stdio / SSE / streamable-http
                            │              · 或 ·
                            │ HTTP REST (带 X-API-Key / Bearer)
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │      适配层  (omnicode_adapters/)                       │
              │  · cli/                — omnicode CLI 子命令            │
              │  · mcp_server/         — FastMCP 宿主 + 鉴权门          │
              │  · agent/              — 本地文件同步 watcher           │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │     FastAPI 应用  (api/v1/routers/* — 22 个 router)     │
              │     中间件: API-key → RBAC → read-only                  │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │  omnicode_core/  ← 语言无关,不依赖 UI / LLM             │
              │  ├── intelligence/composer.py  (八能力合一)             │
              │  ├── ast/  search/  graph/  memory/  edit/  lsp/        │
              │  ├── auth/ (RBAC、迁移、主密钥轮换)                     │
              │  ├── workspace/ (per-workspace 书签)                    │
              │  ├── index/ sharding.py (per-workspace FAISS 分片)      │
              │  ├── embeddings/ (本地 · 远程 · 混合后端)               │
              │  └── security/sandbox.py                                │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │  存储                                                    │
              │  <wd>/.data/shards/<id>/  vector_store.faiss + .db,     │
              │                           file_tracker.db、snapshots/、 │
              │                           edit_sessions/                 │
              │  ~/.kiro/codebase-mcp/    providers.db、users.db、      │
              │                           workspaces.json                │
              └─────────────────────────────────────────────────────────┘
```

三层、单向。适配层调 core,core 永远不导入适配层。LLM 与 Web UI 都是
**可选 extras** — 精简安装下 core 一样能跑。完整设计动机见
[`docs/architecture.md`](docs/architecture.md)。

### 三种部署模式

| 模式 | 代码位置 | 服务器做什么 | 默认开关 |
|---|---|---|---|
| 🏠 **local** | 本机 | 索引、搜索、LSP、编辑 | 写入 ✅,apply ✅ |
| ☁️ **cloud** | 本机(镜像) | 索引、搜索、LSP、~编辑~ | 写入 ❌,apply ❌ |
| 🔄 **hybrid** | 用户本地 | 索引 + 搜索 + 记忆 + 图;agent 推送文件 | 经 agent 写入 ✅,apply ❌ |

生产云部署见 [`docs/deployment.md`](docs/deployment.md) — systemd + nginx
方案、Docker Compose + Caddy 方案、八层安全模型、加固清单。

---

## 🔧 MCP 工具

默认 8 个核心工具 (`OMNICODE_MCP_TOOLS=core`):

| 工具 | 适合问什么 |
|---|---|
| `omni_search` | "帮我找到 auth 中间件在哪初始化" — 语义 / 符号 / 文本 / hybrid / LSP references |
| `omni_read` | "给我 `services/billing.py` 的大纲" — outline / symbols / full / range / imports / diagnostics |
| `omni_impact` | "我把 `User.email` 重命名了会炸什么?" — 调用方、被调方、风险徽章、推荐测试 |
| `omni_diagnostics` | "`api/users.py` 现在有什么问题?" — ruff / mypy / eslint / tsc / LSP 诊断融合 |
| `omni_context` | "把解释 `create_app` 需要的所有上下文给我" — composer 一次调完 |
| `omni_memory` | "这个仓库以前有人解决过类似问题吗?" — 用户存入的记忆 + 多角度自动召回 |
| `omni_patch` | "把这段补丁安全应用上去" — preview → validate → apply → rollback,带 EditSession id 可撤销 |
| `discover_tools` | 列出工具表面,挑一个最合适的 |

向后兼容别名(老 MCP 配置仍能工作):
`omni_analyze` → `omni_impact`,`omni_edit` → `omni_patch`,
`omni_intelligence` → `omni_context`。

需要更细粒度的工具? `OMNICODE_MCP_TOOLS=all` 暴露 24 个(8 个核心 + 16 个
低层旧版); `legacy` 只暴露 16 个低层。

---

## 💡 使用场景

OmniCode-MCP 故意做成一个 **底层服务**,不是产品。可以在它上面构建:

- **更聪明的 AI 编辑器扩展** — 每次 prompt 前先调 `/intelligence/context`,
  token 砍 30 – 60% 同时提升准确率。
- **PR 评审机器人** — 在每个 diff 上跑
  `/graph/impact?symbol=…&depth=3`,把高影响半径的改动标出来给人审。
- **重构 Agent** — 用 agent 自己的 LLM 在 `/patch/preview` →
  `/patch/validate` → `/patch/apply` 上循环,LLM 永远不直接落盘。
- **CI 质量门禁** — PR 改了 `risk_level=high` 的符号但没在
  suggested-tests 里加测试? 直接拒绝合并。
- **自定义 dashboard** — 自带的 Web 控制台只是公开 REST API 的一个 HTML/JS
  客户端。可以自己造一个。
- **内部文档生成** — 调 `/symbols/graph` 和 `/git/history` 自动写 ADR。

---

## ⚙️ 配置

三个来源,优先级从高到低:

1. **CLI 参数** — `omnicode serve --mode cloud --port 8765 ...`
2. **环境变量** — 每个 Pydantic 字段都有同名 env (例如 `OMNICODE_PORT`、
   `OMNICODE_API_KEY`)。
3. **TOML 文件** — 启动目录下的 `omnicode.toml`(或通过
   `OMNICODE_CONFIG=/path` 指定)。见
   [`omnicode.example.toml`](omnicode.example.toml)。

最常用的几个开关:

```toml
[server]
mode = "local"            # local | cloud | hybrid
host = "127.0.0.1"
port = 6789

[security]
api_key = ""              # 旧版单密钥鉴权 (X-API-Key 头)
allow_apply_patch = true  # 云部署常常关掉
mcp_tools = "core"        # core (8) | all (24) | legacy (16)

[index]
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"

[search]
reranker = false          # 交叉编码器重排序(opt-in)

[features]
web_console = true
lsp = true
memory = true
safe_edit = true
llm_router = true         # 设 false 走纯核心(无 LLM)安装
ai_edit = true            # LLM 驱动的 /edit 端点;依赖 llm_router
```

完整参考 — 每个 env 变量、每个 TOML 键、每条优先级规则 — 都在
[`docs/usage.md`](docs/usage.md)。

---

## 🔒 安全速览

纵深防御,层层 opt-in。本地笔记本可以零鉴权零开销跑,生产云盒上也能开足
全部八层 — 用的是同一份代码。

- **路径沙箱** — `..`、绝对路径、出树 symlink 在每个端点都被拒。
- **写入永远走 PatchManager** — LLM 驱动的 `/edit`、智能 `write`、
  fallback 文件操作全部经 PatchManager,自带 snapshot + EditSession +
  rollback。LLM 不会在不留 breadcrumb 的情况下覆盖你的文件。
- **三档鉴权** — 单 API key、多用户 RBAC (admin / editor / viewer)、
  per-deployment 只读模式,可以叠着用。
- **provider key 加密落盘** — `~/.kiro/codebase-mcp/providers.db` 用 Fernet
  加密。`omnicode rotate-master-key` 在新主密钥下重加密所有行,失败会回滚。
- **token 过期 + 按用户撤销** — 签发时设 `expires_in_days`,过期首次使用
  时自动撤销;一行 `DELETE /admin/users/{u}/tokens` 处理离职员工。
- **MCP-over-HTTP 鉴权门** — SSE / streamable-http 走相同的鉴权来源;
  `--auth required` 模式下没配置鉴权就拒绝启动。
- **per-workspace 分片** — workspace A 的搜索结果不会泄漏到 workspace B;
  删 workspace 同时原子删分片。

> [!IMPORTANT]
> 云模式默认只读 + 拒绝 apply。preview / validate / explain 仍然开放,
> 编辑器可以把 diff 和分析渲染给用户看,但绝不会通过网络直接写盘。

完整安全模型与威胁模型见
[`docs/deployment.md`](docs/deployment.md)。

---

## 🖥️ CLI

```bash
omnicode init                      # 写 .data/ 骨架
omnicode index [--force]           # 增量 / 全量重建
omnicode status                    # 走 /health
omnicode doctor                    # python / LSP / 模型 / 端口体检
omnicode serve [--headless] [--console] [--mode local|cloud|hybrid]
omnicode dev                       # 控制台 + 自动 reload
omnicode mcp                       # 给 AI 编辑器用的 stdio MCP
omnicode agent --remote URL --token TOK --workspace .
omnicode rotate-master-key [--db ...] [--key ...] [--new-key BASE64]
```

[`scripts/`](scripts/) 下的 run-helper (`run.bat` / `.sh`、`run-dev.bat` /
`.sh`、`test.bat` / `.sh`、`lint.bat` / `.sh`) 双击或一行就跑。

---

## 📊 性能

在本仓库验证过(~125 个源文件,大部分是 Python):

| 基准 | 目标 | 实测 |
|---|---|---|
| 调用图冷启动构建 | < 1.5 s | **702 ms** |
| 调用图 `update_file` 中位数 | < 50 ms | **10 ms** |
| 继承图冷启动构建 | < 1 s | **503 ms** |
| 继承图 `update_file` | < 20 ms | **2 ms** |
| Token 压缩 5 KB | < 10 ms | **2 – 2.5 ms** |
| 增量重建(无文件改动) | — | **< 1 s** |

跑 `python benchmarks/run_all.py` 复现。

---

## 🧪 测试

```bash
# 全套(~30 秒)
python -m pytest tests -q

# 仅回归环(~12 秒)
python -m pytest tests/integration/test_route_regressions.py -q

# Lint
ruff check omnicode omnicode_core omnicode_adapters api core tests
```

最新 CI: **433 通过、12 跳过** — 那 12 个是 LSP 二进制探测,本地没装对应
language server 时自动跳过。

---

## 📚 文档

五份文档,一目了然:

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 系统都有什么 — 设计动机、八种能力、模块图、持久化布局。 |
| [`docs/usage.md`](docs/usage.md) | 安装、配置、运行、接入 AI 编辑器,含 LLM extras。 |
| [`docs/api.md`](docs/api.md) | 完整 REST + MCP 目录,含请求/响应结构。 |
| [`docs/deployment.md`](docs/deployment.md) | 生产部署方案 + 八层安全模型。 |
| [`docs/roadmap.md`](docs/roadmap.md) | 1.0 之后的研究方向、永久 non-goals。 |
| [`extensions/vscode/README.md`](extensions/vscode/README.md) | 极简 VS Code 扩展(3 个命令)。 |
| [`_keep_/README.md`](_keep_/README.md) | 如何分享被 `.gitignore` 默认忽略的产物。 |

---

## 🗺️ 路线图

原始 architecture-v2 + Wave 1 审计 + Wave 2 待办(共 43 项)在
`1.0.0-rc1` 已 **全部完成**。1.0 之后的研究方向在
[`docs/roadmap.md`](docs/roadmap.md):

- 🧠 代码专用 embedding (Jina v3 / OpenAI / starcoder),配 A/B harness 与
  当前通用编码器对比。
- 📦 Skills 框架对齐 (Anthropic Agent Skills) — 让编辑器按任务只加载需要
  的工具子集。
- 🛡️ 代码执行沙箱 (`bubblewrap` / `seccomp`),给目前禁用的
  `execute_tool` 用。
- 📈 Telemetry 驱动的 prompt 反馈 — 把编辑会话挖进记忆建议库,完全
  opt-in。

---

## ❓ 常见问题

<details>
<summary><b>这跟 Cursor / Continue / Claude Code 有什么不同?</b></summary>

OmniCode-MCP 是 **服务**,不是编辑器。Cursor 和它们写代码;OmniCode-MCP
通过 MCP 给它们提供搜索、影响分析、安全打补丁、记忆召回,让它们写得更
准。OmniCode-MCP 可以和这些编辑器同时使用。
</details>

<details>
<summary><b>必须用付费 LLM 吗?</b></summary>

不需要。core(搜索、影响、补丁、记忆、MCP 表面)不依赖任何 LLM 就能跑。
LLM 路由器是 **可选 extra** (`pip install -e ".[llm]"`),给那些想要
服务端 AI edit pipeline 的用户。多数用户让自己的 AI 编辑器(Claude /
GPT / Gemini)直接调 OmniCode-MCP 的工具。
</details>

<details>
<summary><b>我的代码会离开本机吗?</b></summary>

默认不会。本地模式从不外发请求。embedding 用
`sentence-transformers/all-MiniLM-L6-v2` 在本机跑。可选的远程 embedding
后端和可选的 LLM 路由器是仅有的两条会跟第三方说话的路径,默认都关闭。
</details>

<details>
<summary><b>支持哪些语言?</b></summary>

**AST**: Python、JavaScript、TypeScript、C++、Java、Go、Rust (7 种)。
**LSP**: Python、TypeScript、Go、Rust、C/C++、Ruby、PHP、Java、Kotlin、
C# (10 种) — 自动检测,bridge 在没装对应 server 时跳过。
</details>

<details>
<summary><b>能在 Windows 上跑吗?</b></summary>

可以。在 Windows 10/11 + Python 3.11 (conda 或 venv) 上测过。最简单
直接用 `scripts/run.bat`。路径沙箱正确处理盘符,在不允许创建 symlink
的情况下会优雅降级。
</details>

<details>
<summary><b>怎么扩展规模?</b></summary>

per-workspace FAISS 分片让多项目部署彼此隔离。冷启动构建成本基本与源
文件数线性相关;增量缓存让热重载几乎零开销。推荐的云形态是每个团队
一台 VM — 多租户 SaaS 不是目标。
</details>

<details>
<summary><b>embedding 模型的许可怎么算?</b></summary>

`sentence-transformers/all-MiniLM-L6-v2` 是 Apache 2.0。Tree-sitter 文法
是 MIT。LSP server 各自携带自己的许可证,以独立进程加载。OmniCode-MCP
自己的代码是 MIT。
</details>

---

## 🤝 贡献

欢迎 PR。简易清单:

1. push 前跑 `ruff check omnicode omnicode_core omnicode_adapters api core tests`
   — 永远不要对 `tests/` 用 `--fix`。
2. UI 可见的修复必须配回归测试。
3. API 响应保持 `{"success": true, "result": {...}}` 信封,Web 客户端的
   handler 才不会炸。
4. 架构层的改动写到 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

完整开发者上手 — 架构规则、编码规范、常用模式、回归测试矩阵 — 都在
[`CONTRIBUTING.md`](CONTRIBUTING.md)。

---

## 📄 许可

MIT — 见 [`LICENSE`](LICENSE)。

---

## 🙏 致谢

设计参考下列资料,内容已为合规改写、压缩。

**规范与协议**

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Tree-sitter](https://tree-sitter.github.io/tree-sitter/)
- [Language Server Protocol](https://microsoft.github.io/language-server-protocol/)

**Anthropic 工程博客**

- [Code execution with MCP — building more efficient AI agents](https://anthropic.com/engineering/code-execution-with-mcp), 2025-11
- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09
- [Advanced tool use on the Claude Developer Platform](https://www.anthropic.com/engineering/advanced-tool-use), 2025-11

**Token 效率与 MCP 优化**

- [StackOne — MCP Token Optimization](https://www.stackone.com/blog/mcp-token-optimization/)
- [MindStudio — MCP Optimization Techniques](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization)
- [Atlassian Labs — mcp-compressor](https://github.com/atlassian-labs/mcp-compressor)

**LSP 在 MCP 中的集成**

- [jonrad/lsp-mcp](https://github.com/jonrad/lsp-mcp)
- [Skywork — lsp-mcp bridging MCP and LSP](https://skywork.ai/blog/lsp-mcp-mcp-lsp-bridge/)

**上游**

- [danyQe/codebase-mcp](https://github.com/danyQe/codebase-mcp) — 本 fork
  的源项目。

**依赖库**

[LiteLLM](https://github.com/BerriAI/litellm) ·
[FAISS](https://github.com/facebookresearch/faiss) ·
[sentence-transformers](https://github.com/UKPLab/sentence-transformers) ·
[FastAPI](https://fastapi.tiangolo.com/) ·
[Pydantic v2](https://docs.pydantic.dev/) ·
[D3.js v7](https://d3js.org/) ·
[highlight.js](https://highlightjs.org/) ·
[Tailwind CSS](https://tailwindcss.com/) ·
[tree-sitter](https://tree-sitter.github.io/) ·
[cryptography (Fernet)](https://cryptography.io/) ·
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) ·
[watchfiles](https://github.com/samuelcolvin/watchfiles)

> 所有第三方商标归各自所有者所有。引用为署名;本仓库代码是基于
> 上述文献中思想的原创实现。

---

<div align="center">

如果 OmniCode-MCP 帮你省了 token 或避免了一次翻车,点个 ⭐ 让其他人也能找
到它。

</div>
