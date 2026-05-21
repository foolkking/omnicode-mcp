# OmniCode-MCP 🚀

> **OmniCode-MCP** 是一款专为 AI 辅助软件工程设计的高阶、企业级 Codebase 理解与操作 MCP（Model Context Protocol）服务端。
> 它是对传统代码理解工具的革命性升级，旨在通过引入**时间维度（Git 历史感知）**、**高精度跨语言语法树（Tree-sitter AST）**、**全天候多模型智能路由网关**以及**主动静态分析代码质量门禁**，为 Claude Desktop、Cursor、VS Code 等主流 AI 终端提供无可比拟的代码阅读与编写上下文。

---

## ✨ 核心创新特性 (Premium Features)

### 1. 🌌 四维时空代码溯源：Git-Aware Context
*   **痛点**：传统 AI 工具仅能感知当前代码的静态空间，在修改老旧代码时，经常由于“无知”而破坏前人为了应对某个隐蔽边缘 Bug 而特意编写的看似“不合理”但实则极其关键的防御性补丁逻辑。
*   **创新**：集成了 Git 深度分析模块。当 AI 请求或修改某段代码时，MCP 不仅能提取最新的代码段，还能自动注入该段代码的 `git blame` 历史、最近关联的 Commit Messages，甚至是该次变更关联的 Issue/PR 记录，让大模型在修改代码前洞悉其背后的“历史因果”。

### 2. 🌳 精准跨语言抽象语法树检索：AST-Driven RAG
*   **痛点**：基于正则表达式或单纯以行数切分的 Code Chunker 会在遇到 JS/TS 的复杂模板字符串或 C++ / Rust 的高级嵌套宏时发生碎裂，导致上下文缺失或提取错误。
*   **创新**：全面集成 `tree-sitter`，支持 **Python, JS, TS, C++, Java, Go, Rust** 7 种语言的高精度 AST 解析。不再依赖粗暴的正则，而是以真实的类、函数、块级 AST 边界为单位进行分块，并自动合并导入依赖，生成超高精度的局部调用关系图谱。

### 3. 🚦 全天候智能模型路由网关：Multi-API Router
*   **痛点**：单一模型 API 的硬编码（如强绑定单一 API 密钥或模型）无法兼顾开发过程中的“高智商任务”与“海量扫库任务”，且在遇到 API 频控 (Rate Limit) 或高并发时容易发生死锁。
*   **创新**：集成 `LiteLLM` 的强健模型网关抽象。支持多厂商（Anthropic, OpenAI, DeepSeek, Gemini, Ollama 本地模型）API 密钥的统一热插拔。内建智能分级路由策略（如 `CostOptimized` 低成本扫库、`QualityFirst` 复杂重构），并支持指数退避的优雅降级重试与备用 API 自动 Fallback。

### 4. ✂️ 智能上下文剪裁：Smart Token Compressor
*   **痛点**：全量发送依赖代码会导致上下文迅速膨胀，既容易爆掉模型的 Token 限制，又会带来极高的 API 账单开销。
*   **创新**：引入 Tiktoken 精密 Token 计算拦截器。当发现提取的上下文超过目标模型的 Context Window 时，执行动态剪裁：保留核心逻辑与指令 ➡️ 规范化无关的空白符 ➡️ 合并重复的导入语句 ➡️ 剥离纯修饰性注释（保留 TODO/FIXME）➡️ 折叠不相关的函数体为单行签名，最大化地榨取每一位 Token 的黄金价值。

### 5. 🛡️ 主动防御质量门禁：Proactive Guard
*   **痛点**：AI 生成的代码可能存在低级的语法错误、类型不匹配或内存泄露，导致本地构建频繁失败，折损开发效率。
*   **创新**：集成了本地静态分析防御。在 AI 修改/生成文件后，静默调用相应的工具（Python ➡️ mypy/ruff；JS/TS ➡️ eslint/tsc；C++ ➡️ cppcheck）进行即时诊断。若检测到致命错误，系统将触发**自动纠偏反馈循环 (Feedback Loop)**，把诊断报告重新作为上下文投喂给大模型进行自我修正，确保输出到您本地的代码百分之百能通过构建！

---

## 🛠️ 项目架构设计 (Architecture)

`OmniCode-MCP` 继承并优化了 **FastMCP Gateway (stdio) ⬅️➡️ FastAPI (HTTP REST)** 的双层解耦拓扑：

```
                +-----------------------------------------+
                |          Any MCP Client                 |
                |   (Claude Desktop, VS Code, Cursor)     |
                +-----------------------------------------+
                                     |
                                  (stdio)
                                     v
                +-----------------------------------------+
                |         mcp_server.py (MCP Server)      |
                +-----------------------------------------+
                                     |
                                (HTTP REST)
                                     v
                +-----------------------------------------+
                |        FastAPI Backend (:6789)          |
                +-----------------------------------------+
                     /               |               \
                    /                |                \
    +-----------------+     +-----------------+     +-----------------+
    |   omnicode/     |     |   omnicode/     |     |   omnicode/     |
    |   llm/          |     |   ast_engine/   |     |   guard/        |
    |  - MultiRouter  |     |  - Tree-sitter  |     |  - mypy/ruff    |
    |  - Compressor   |     |  - AST Chunker  |     |  - eslint/tsc   |
    +-----------------+     +-----------------+     +-----------------+
            |                        |                       |
      (Claude/OpenAI/         (FAISS / Hybrid        (Automatic Error
     DeepSeek/Gemini/Ollama)    Semantic Search)      Feedback Loop)
```

---

## 🚀 部署与快速上手 (Quick Start)

具体部署与环境搭建细节，请参阅我们为您准备的持续更新持久化文档：  
📖 **[deployment_build_record.md](deployment_build_record.md)**

### 1. 克隆与配置
```powershell
# 克隆本项目
git clone https://github.com/your-username/omnicode-mcp.git
cd omnicode-mcp

# 准备环境变量
copy .env.example .env
```
*(请在 `.env` 中填入您要激活的模型的 API Key)*

### 2. 本地虚拟环境一键构建
```powershell
# 创建虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 以可编辑模式安装所有依赖（由 pyproject.toml 声明）
pip install -e .
```

### 3. 运行 API 服务与 MCP 网关
*   **后端启动**：
    ```powershell
    uvicorn main:app --port 6789 --reload
    ```
*   **Claude Desktop 配置**：
    在 `C:/Users/<Username>/AppData/Roaming/Claude/claude_desktop_config.json` 中配置您的 stdio 入口：
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

## 🤝 参与贡献 (Contributing)

欢迎任何形式的 Issue、PR 与架构讨论！详细开发约定及模块设计哲学，请阅读：  
📄 **[CONTRIBUTING.md](CONTRIBUTING.md)**

---
*OmniCode-MCP — 让您的 AI 助手拥有完美的时间与空间代码视野。*
