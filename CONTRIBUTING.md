# Contributing to OmniCode-MCP 🌟

首先，非常感谢您对参与 **OmniCode-MCP** 贡献感兴趣！您的参与将使这款基于 MCP 协议的智能代码助手的体验变得更加非凡。

---

## 📋 目录 (Table of Contents)

*   [我们的愿景](#-我们的愿景)
*   [开发环境搭建 (Development Setup)](#-开发环境搭建-development-setup)
*   [项目结构贡献指南](#-项目结构贡献指南)
*   [编码与代码规范 (Coding Guidelines)](#-编码与代码规范-coding-guidelines)
*   [测试与静态检查](#-测试与静态检查)
*   [提交变更指南 (Pull Request)](#-提交变更指南-pull-request)

---

## 🌌 我们的愿景

`OmniCode-MCP` 旨在打造一款最强健、最懂开发者意图的工程级 Codebase 理解与操作 MCP 服务端。我们通过引入**时空四维感知 (Git Blame & History)**、**跨语言精确语法树分析 (Tree-sitter AST)**、**全厂商模型网关路由器 (LiteLLM Router)** 以及**静默编译级静态分析防护网关 (Proactive Guard)**，极大地拓宽了 AI 在处理大型老旧工程代码时的上下文视野。

---

## 🛠️ 开发环境搭建 (Development Setup)

### 1. 配置前置环境
*   **Python**: `>= 3.11`
*   **C++ 编译器**: (在 Windows 上编译 tree-sitter C++ 等解析包时可能需要 VS Build Tools)

### 2. 克隆项目与安装
```powershell
# 克隆工作区代码
git clone https://github.com/your-username/omnicode-mcp.git
cd omnicode-mcp

# 初始化 Python 虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 以本地可编辑状态安装核心包及依赖（依据 pyproject.toml 声明）
pip install -e .[dev]
```

---

## 📦 项目结构贡献指南

项目采用了全新重构的插件化、模块化包架构。在贡献代码时，请遵循以下模块职能约定：

```
omnicode-mcp/
├── omnicode/               # 核心服务包目录
│   ├── config/             # Pydantic 强类型全局设置
│   ├── llm/                # 多模型路由、降级重试与 Tiktoken 压缩管理器
│   │   └── providers/      # 各种云端与本地大模型 Provider
│   ├── ast_engine/         # Tree-sitter AST 解析引擎与智能分块器
│   ├── git_context/        # Git Blame 信息收集与 Issue/PR 时空关联
│   ├── guard/              # mypy/ruff/eslint 静态分析防护与 Feedback 反馈循环
│   ├── search/             # 混合搜索引擎（FAISS 向量匹配 + BM25 关键词）
│   └── server/             # MCP stdio 协议核心网关及工具定义
├── api/                    # 待对接的 FastAPI Web API 路由器接口
├── core/                   # 依赖注入和 lifespan 管理器（开发中）
├── templates/              # FastAPI 本地 Dashboard Web UI 模板
└── pyproject.toml          # 项目构建描述与模块依赖管理
```

*   **新增 LLM 供应商支持**：请在 `omnicode/llm/providers/` 中创建对应的 provider 类，继承并实现 `BaseLLMProvider`。
*   **扩展新编程语言语法分析**：请在 `omnicode/ast_engine/` 中配置相应的 `tree-sitter-<language>` 并更新 `parser.py` 与 `chunker.py`。
*   **添加新静态防御工具**：请在 `omnicode/guard/tools/` 目录下添加您要集成的本地 CLI 编译器检查工具。

---

## 🎨 编码与代码规范 (Coding Guidelines)

我们使用 [ruff](https://github.com/astral-sh/ruff) 来进行代码风格检查与代码格式化。在提交易受审查前，请确保您本地的代码风格规范符合要求：
*   **类型声明 (Type Hints)**：对于新编写 of `omnicode/` 导出 API 模块，必须提供完整且强类型的 Type Hint 注释。
*   **单例模式规范**：对于全局共享的 AST 引擎及 LLM 路由，请统一从全局 `core` 注入层中获取，切忌在子类中进行重复实例化。

---

## 🧪 测试与静态检查

我们使用 `pytest` 运行单元测试与集成测试，并使用 `ruff` 以及 `mypy` 作静态代码门禁把关。

```powershell
# 1. 运行代码规范检查
ruff check omnicode/

# 2. 运行强类型静态分析检查
mypy omnicode/ --strict

# 3. 运行单元测试
pytest tests/
```

---

## 🚀 提交变更指南 (Pull Request)

1.  请确保您的代码百分之百能够通过本地的 `ruff check`、`mypy` 类型验证以及全量单元测试。
2.  请在您的 PR 描述中清晰说明所作的优化（例如：新增了哪种语言的 tree-sitter AST 支持，或是优化了 LiteLLM 的何种策略）。
3.  如果引入了破坏性变更（例如修改了核心环境变量的配置键名），请务必同步更新本地的持续构建部署文档 `deployment_build_record.md` 并在 PR 中作出醒目说明。

---
*共同构建最智慧的 MCP 开发未来！*
