# OmniCode-MCP 部署与构建记录 (Deployment & Build Record)

> **这是一个持续更新的持久化文档**，用于记录项目在从原始架构重构升级至 `OmniCode-MCP` 过程中的构建状态、部署拓扑、废弃清理记录、以及后续集成步骤。

---

## 1. 构建状态与路线图 (Build Status & Roadmap)

| 阶段 | 模块/功能 | 状态 | 对应新模块 | 最近变更描述 |
| :--- | :--- | :---: | :--- | :--- |
| **Phase 1** | 项目基础架构重构 | 🟢 已完成 | `omnicode/config` | 升级 `pydantic-settings`，引入 `pyproject.toml`，废弃 `requirements.txt` |
| **Phase 2** | Multi-API 路由网关 | 🟢 已完成 | `omnicode/llm` | 完成 LiteLLM 路由、Provider 注册和降级重试逻辑 |
| **Phase 3** | AST-Driven RAG 引擎 | 🟢 已完成 | `omnicode/ast_engine` | 引入 `tree-sitter` 多语言语法解析与智能分块 |
| **Phase 4** | 智能 Token 压缩器 | 🟢 已完成 | `omnicode/llm` | 实现 tiktoken 计数器与动态上下文压缩裁剪策略 |
| **Phase 5** | Git 溯源与主动防御门禁 | 🟢 已完成 | `omnicode/git_context`, `guard` | 完成 Git Blame 信息解析与 Proactive Guard 自动纠偏循环 |
| **Phase 6** | MCP 工具层与 API 桥接 | 🟢 已完成 | `omnicode/server` | 彻底清理废弃代码，路由桥接至 `omnicode` 内核，解决 Tree-sitter 0.22+ 兼容性，且通过 HTTP 状态端点端对端验证 |

---

## 2. 代码库清理记录 (Codebase Cleanup Log)

为了实现彻底的插件化和高内聚架构，我们对原始项目代码进行了“去粗取精”的清理。

### 🗑️ 已物理删除的废弃模块 (Deprecated & Cleaned)
*   **`chunkers/`**：原基于正则的 JS/TS、Python 分块器，已被 `omnicode/ast_engine/chunker.py`（基于 Tree-sitter AST 精确分块）完全替代。
*   **`code_tools/`**：原 Gemini 专属的客户端和编写/编辑 Pipeline，已被多模型路由网关 `omnicode/llm/` 和静态分析防御网关 `omnicode/guard/` 替代。
*   **`semantic_search/`**：原搜索引擎实现，已被混合检索系统 `omnicode/search/` 代替。
*   **`requirements.txt`**：已废弃，由现代的 `pyproject.toml` 进行统一声明和模块化依赖管理。

### 📦 保留并待重构的复用模块 (Retained & To Be Wired)
*   **`api/`**：FastAPI REST 接口路由。需修改其导入路径，由旧的 `code_tools` / `semantic_search` 桥接到新的 `omnicode/` 组件。
*   **`core/`**：FastAPI app 生命周期及依赖注入管理器。
*   **`memory_system/`**：持久化记忆层（SQLite 线性扫描）。将在此记录并正常桥接，后续逐步重构到 `omnicode/memory`。
*   **`schemas/`, `templates/`, `utils/`**：FastAPI 的数据格式定义、静态 HTML 模版和工具函数，为无状态模块，将全量复用。
*   **`main.py`**：FastAPI 后端入口。
*   **`mcp_server.py`**：MCP Server 协议入口。

---

## 3. 最近几步该怎么做：桥接与集成指南 (Integration Steps)

为了恢复服务的正常运行，**最近几步** 必须对保留的复用层代码进行修改，将它们重新“缝合”到 `omnicode` 核心服务上。

### 🔄 步骤 3.1: 重构 `core/dependencies.py`
需要将原先从 `code_tools`、`semantic_search` 导入的依赖，重定向为从 `omnicode` 中获取：
*   原 `SemanticSearchEngine` ➡️ 桥接为 `omnicode.search.engine.SearchEngine`
*   原 `EditPipeline` 和 `WritePipeline` ➡️ 桥接为 `omnicode.pipelines.edit.EditPipeline` / `write.WritePipeline`

### 🔄 步骤 3.2: 重构 `core/lifespan.py`
重构 FastAPI lifespan 管理器：
*   在 `startup` 时，初始化 `omnicode` 基础配置、模型网关路由，以及 Tree-sitter 多语言解析器。
*   将原有的 FAISS 向量数据库预热，替换为 `omnicode` 的 Hybrid 混合检索初始化。

### 🔄 步骤 3.3: 适配 `api/v1/routers/` 路由层
逐一更新以下路由器，修改其导入和调用逻辑：
1.  **`files.py`**：修改 `edit` / `write` API 终点，调用 `omnicode.pipelines` 服务。
2.  **`search.py`**：修改 `query` 终点，调用 `omnicode.search` 混合检索。
3.  **`git.py`**：利用 `omnicode.git_context.blame.GitBlameAnalyzer`，为 `git` 路由注入“四维时空”溯源上下文。

### 🔄 步骤 3.4: 更新双层代理 `mcp_server.py`
*   在 `mcp_server.py` 中更新工具定义，将 MCP 请求映射到最新的 `omnicode` 微服务。
*   配置 `smart_edit`、`code_graph_tool` 等新工具，释放 OmniCode-MCP 的高阶创新点。

---

## 4. 本地构建与部署指南 (Local Build & Deploy)

### 📋 前置要求 (Prerequisites)
*   **Python**: `>= 3.11`
*   **C++ Compiler**: 编译 `tree-sitter` 各语言解析器时可能需要（Windows 推荐安装 VS Build Tools 2022）。

### 🛠️ 构建步骤 (Build Steps)

1.  **初始化虚拟环境并安装依赖**
    ```powershell
    # 创建虚拟环境
    python -m venv .venv
    # 激活虚拟环境
    .venv\Scripts\Activate.ps1
    # 安装包以及所有依赖（使用 pyproject.toml 自动解析）
    pip install -e .
    ```

2.  **配置环境变量**
    复制 `.env.example` 并重命名为 `.env`：
    ```powershell
    Copy-Item .env.example .env
    ```
    配置您需要的 API Key（如 `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` 等）。

3.  **启动 FastAPI 后端服务**
    ```powershell
    # 通过 uvicorn 启动
    uvicorn main:app --port 6789 --reload
    ```

4.  **注册 / 运行 MCP Server**
    您可以使用 Claude Desktop 进行集成，在其配置文件（`config.json`）中添加：
    ```json
    "mcpServers": {
      "omnicode-mcp": {
        "command": "python",
        "args": ["/path/to/omnicode-mcp/mcp_server.py"],
        "env": {
          "ENV_FILE": "/path/to/omnicode-mcp/.env"
        }
      }
    }
    ```

---
*文档更新于：2026-05-21，将随着每一次构建与重构提交而持续更新。*
