# 本地运行指南

> 本文档给出 OmniCode-MCP 在本地的所有运行方式。所有命令都假设 conda 环境名为
> `omnicode-env`（可以通过 `CONDA_ENV_NAME` 环境变量覆盖）。如果你用 `venv` 而不是
> conda，把每条命令里的 `conda run -n omnicode-env python` 替换成
> `python`(在已激活的 venv 内)即可。

## 0. 前置准备

```bash
# 创建并激活环境(只需一次)
conda create -n omnicode-env python=3.11 -y
conda activate omnicode-env
pip install -e .

# 或者用 venv
python -m venv .venv
. .venv/Scripts/activate    # Windows PowerShell
# . .venv/bin/activate      # macOS / Linux
pip install -e .
```

环境变量(可选):

```bash
copy .env.example .env
# 编辑 .env 填写你的 API key,或者通过 Web UI 的 Model Providers 面板添加
```

---

## 1. 启动 Web 后端(最常用)

### Windows (cmd / PowerShell)

```cmd
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 6789
```

或者激活环境后直接跑:

```cmd
conda activate omnicode-env
uvicorn main:app --port 6789
```

启动需要 5-10 秒(加载 sentence-transformers 模型 + tree-sitter parsers)。
看到日志里出现 `🚀 All services initialized successfully` 就可以打开浏览器了。

### macOS / Linux

```bash
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 6789
```

**访问地址**: <http://127.0.0.1:6789/>

第一次使用先去 **Search & Index** 面板点一次 **Rebuild Index**(约 30-60 秒)
来构建语义索引。

### 开发模式(代码改动自动 reload)

```cmd
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 6789 --reload
```

### 监听所有网卡(让别的机器访问)

```cmd
conda run --no-capture-output -n omnicode-env uvicorn main:app --host 0.0.0.0 --port 6789
```

---

## 2. 接入 Claude Desktop / Cursor / VS Code

OmniCode 通过 stdio 给 MCP 客户端提供 25+ 个工具。**Web 后端(6789 端口)
必须同时在跑**——MCP server 内部走 HTTP 调用它。

### 找到 Python 解释器路径

```cmd
:: Windows
conda activate omnicode-env
where python

:: 输出形如:E:\anaconda\envs\omnicode-env\python.exe
```

```bash
# macOS / Linux
conda activate omnicode-env
which python

# 输出形如:/Users/you/miniconda3/envs/omnicode-env/bin/python
```

### Claude Desktop 配置

编辑 `claude_desktop_config.json`(Windows: `%APPDATA%\Claude\`,macOS:
`~/Library/Application Support/Claude/`):

```json
{
  "mcpServers": {
    "omnicode": {
      "command": "<你刚才查到的 python 路径>",
      "args": [
        "<项目根目录绝对路径>/mcp_server.py"
      ],
      "env": {
        "ENV_FILE": "<项目根目录绝对路径>/.env"
      }
    }
  }
}
```

Windows 的反斜杠要双写,例如:

```json
"command": "E:\\anaconda\\envs\\omnicode-env\\python.exe",
"args": ["C:\\path\\to\\codebase-mcp\\mcp_server.py"]
```

保存后**完全退出 Claude Desktop**(任务栏图标右键 Quit),重新打开。
对话框输入 `/` 应该看到 `omnicode` 工具列表。

### Cursor / VS Code

跟 Claude Desktop 同样的 JSON 结构,放进各自的 MCP 配置文件即可。

---

## 3. 测试

### 全部测试

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests -q
```

预期: `267 passed, 1 skipped`,约 90 秒。

### 只跑回归测试套件(快)

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests/integration/test_route_regressions.py -v
```

### 只跑某个文件

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests/unit/test_provider_registry.py -v
```

### 跑某个 case

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests/integration/test_route_regressions.py::test_symbol_search_finds_chunker_metadata_match -v
```

### 收集模式(只列出 case 不执行)

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests --collect-only -q
```

---

## 4. Lint 和静态检查

```cmd
:: ruff(注意:不要对 tests/ 目录用 --fix)
conda run --no-capture-output -n omnicode-env ruff check omnicode api core tests

:: mypy(可选)
conda run --no-capture-output -n omnicode-env mypy omnicode api core
```

> ⚠️ **永远不要对 tests/ 目录跑 ruff --fix**——历史教训:之前一次自动修复
> 误删了整个 tests 目录。只对实现代码用 `--fix`,tests 用纯 `check`。

---

## 5. 性能基准

```cmd
conda run --no-capture-output -n omnicode-env python benchmarks/run_all.py
```

输出会展示 call graph 构建、增量更新、token 压缩等关键路径的耗时。
本仓库 ~125 个文件下的目标:

| 基准 | 目标 | 实测 |
|---|---|---|
| Call graph cold build | < 1.5 s | 702 ms |
| Call graph update_file 中位数 | < 50 ms | 10 ms |
| Inheritance cold build | < 1 s | 503 ms |
| Token compress 5 KB | < 10 ms | 2-2.5 ms |

---

## 6. 单独运行 MCP server(调试用)

```cmd
conda run --no-capture-output -n omnicode-env python mcp_server.py
```

按 Ctrl+C 退出。一般不用手动跑——Claude Desktop / Cursor 会自动 spawn。

---

## 7. 推荐工作流

打开**两个**终端,都 `cd` 到项目根目录。

**终端 1** —— 后端,保持开着:

```cmd
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 6789 --reload
```

**终端 2** —— 跑命令、看日志:

```cmd
conda activate omnicode-env
:: 在这里跑测试、ruff、git 等
```

浏览器打开 <http://127.0.0.1:6789/>。

---

## 8. 一键脚本

项目根目录下提供了开箱即用的脚本:

| 脚本 | 用途 | 平台 |
|---|---|---|
| `scripts/run.bat` | 启动 Web 后端 | Windows |
| `scripts/run-dev.bat` | 启动 Web 后端 (auto-reload) | Windows |
| `scripts/test.bat` | 跑全部测试 | Windows |
| `scripts/lint.bat` | 跑 ruff 检查 | Windows |
| `scripts/run.sh` | 启动 Web 后端 | macOS / Linux |
| `scripts/run-dev.sh` | 启动 Web 后端 (auto-reload) | macOS / Linux |
| `scripts/test.sh` | 跑全部测试 | macOS / Linux |
| `scripts/lint.sh` | 跑 ruff 检查 | macOS / Linux |

Windows 用户双击 `.bat` 即可,macOS / Linux 用户:

```bash
chmod +x scripts/*.sh
./scripts/run.sh
```

如果你的 conda 环境不叫 `omnicode-env`,在调用前设环境变量:

```cmd
:: Windows
set CONDA_ENV_NAME=my-env
scripts\run.bat
```

```bash
# macOS / Linux
CONDA_ENV_NAME=my-env ./scripts/run.sh
```

---

## 9. 常见问题

### Q: 启动卡在 "Loading SentenceTransformer model"

**A**: 确认 `.env` 里有:

```ini
TRANSFORMERS_OFFLINE=1
HF_HUB_OFFLINE=1
```

首次运行需要联网下载模型(约 90 MB),之后走本地缓存。
缓存路径:`~/.cache/huggingface/`。

### Q: 6789 端口被占用

**A**: 换个端口:

```cmd
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 7000
```

### Q: 浏览器访问空白页

**A**:
1. 检查终端日志是否有 ERROR
2. 强刷浏览器(Ctrl+Shift+R)清缓存
3. 打开 DevTools 看 Console 红字

### Q: Claude Desktop 看不到 omnicode 工具

**A**:
1. 检查 `claude_desktop_config.json` JSON 格式合法(用在线 JSON validator)
2. **完全退出**而不只是关窗口(任务栏图标右键 Quit)
3. Web 后端(6789)必须在跑
4. 看 Claude Desktop 的日志(`%APPDATA%\Claude\logs\`)有没有报错

### Q: Memory / 索引数据存哪里?切换项目会丢吗?

**A**: 每个项目自己一个 `.data/` 目录,内含 `vector_store.db`、
`metadata.db` 等。切换工作目录会自动加载新项目的 `.data/`,**不会丢**。

LLM API key 共享在用户级:`~/.kiro/codebase-mcp/providers.db`,所有项目通用。

### Q: 改了代码后服务不更新

**A**: 加 `--reload` 标志,uvicorn 会自动监听文件变化重启:

```cmd
uvicorn main:app --port 6789 --reload
```

注意:某些路由初始化错误下 `--reload` 也救不了,这时候 Ctrl+C 重启即可。
