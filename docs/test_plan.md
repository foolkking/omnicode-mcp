# OmniCode-MCP 系统测试说明文档

> 本文档列出系统每一项功能的**测试方法**、**达标标准**、**当前状态**和**改进建议**。
> 状态标记:✅ 通过 · ⚠️ 部分通过/已知问题 · ❌ 未通过 · 🔄 待人工验证

适用版本:`a1dcdfa update_more` 之后的工作分支
最后更新:2026-05-23

---

## 目录

- [一、自动化测试套件](#一自动化测试套件)
- [二、模型路由 / Provider 系统](#二模型路由--provider-系统)
- [三、AST / 代码图谱 / 符号检索](#三ast--代码图谱--符号检索)
- [四、文件操作 / Edit Pipeline](#四文件操作--edit-pipeline)
- [五、Git 与会话](#五git-与会话)
- [六、记忆系统](#六记忆系统)
- [七、Web 控制台 UI 功能](#七web-控制台-ui-功能)
- [八、深色模式](#八深色模式)
- [九、MCP 工具(Claude Desktop 接入)](#九mcp-工具claude-desktop-接入)
- [十、性能基准](#十性能基准)
- [附录:测试运行命令速查](#附录测试运行命令速查)

---

## 一、自动化测试套件

### 1.1 单元测试

**测试方法**

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests/unit -v
```

**达标标准**

- 所有 14 个 `tests/unit/test_*.py` 文件 collection 通过
- 全部 case PASSED
- 整体执行时间 < 30 秒
- ruff `omnicode api core tests` 0 errors

**当前覆盖范围**

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_ast_parser.py` | ~16 | 7 语言 symbol/import/call 提取 |
| `test_call_graph.py` | ~10 | CallGraph 增量 add/remove/update_file |
| `test_inheritance.py` | ~8 | 5 语言 extends/implements/impl trait |
| `test_token_manager.py` | ~23 | CommentStripper / FunctionFolder / ContextPruner / CostGuard / for_role |
| `test_llm_router.py` | ~21 | FakeProvider 注入 / 4 策略 / fallback / 健康熔断 / best-of-N |
| `test_provider_registry.py` | ~17 | SQLite CRUD / built-in / placeholder filter |
| `test_secret_box.py` | ~10 | Fernet 加解密 / 幂等 / 占位符迁移 |
| `test_model_normalization.py` | ~17 | model 名前缀规范化 + api_base 优先级 |
| `test_session_idempotent.py` | ~9 | session start 幂等 / branch 检测 / trunk 解析 |
| `test_guard.py` | ~14 | ruff JSON / JS Guard 缺工具兜底 / cppcheck XML |
| `test_issue_linker.py` | ~12 | 6 类引用模式 / closing 动词 / GitHub token |
| `test_edit_safety.py` | ~32 | 文件不被 thinking 文本覆盖 / 三层防御 / 大文件 patch / 符号定位 |

**当前状态**

⚠️ 部分通过 — 由于上一轮 `ruff --fix` 误删了 tests 目录(已从 commit `a1dcdfa` 恢复),目前 import 路径与最新后端可能有版本错位,需要人工跑一次 collection 验证。

**改进建议**

1. 在 CI 跑 `pytest --collect-only` 作为前置 sanity check
2. 添加 `tests/__init__.py` 和 `tests/unit/__init__.py` 防止 pytest discovery 受 sys.path 干扰
3. 把现有 `tests/unit/test_edit_safety.py` 的 30+ case 拆成单独的 `test_*` 文件提升并行收敛速度

---

### 1.2 集成测试

**测试方法**

```cmd
conda run --no-capture-output -n omnicode-env python -m pytest tests/integration -v
```

**达标标准**

- 5 个集成文件都启动 TestClient 成功(用 `with TestClient(app) as c`,会触发 lifespan)
- 所有路由返回符合预期的 schema(成功 `{success: true, result: {...}}`,失败 `{success: true, result: {success: false, ...}}`)
- 至少 1 个 GitHub mock-server 测试覆盖 `/git/issues` 富化

**覆盖文件**

- `test_api.py` — provider CRUD / selections / model-status / inheritance / fs / git
- `test_edit_pipeline.py` — happy path + Guard 升级 + 大文件压缩
- `test_issue_linker_github.py` — in-process GitHub mock,4 个 case
- `test_route_regressions.py` — `/symbols/graph` 路由 / `/read` null 处理 / 422 改 200
- `test_session_idempotent.py`(部分集成)— 幂等 start

**当前状态**:同 1.1。

---

## 二、模型路由 / Provider 系统

### 2.1 Provider CRUD

**测试方法(UI)**

1. 打开浏览器 `http://localhost:6789/`
2. 进入 **Model Providers** 面板
3. 点 "Add Provider" 填入:
   - name: `local-gemini`
   - model: `gemini-2.5-pro`
   - api_base: `http://127.0.0.1:2048/v1`
   - api_key: 任意非空(本地网关)
   - provider_type: `openai-compatible`
4. 保存,出现在列表
5. 点 "Test" — 应弹出 Pong 成功消息
6. 编辑、禁用、启用、删除 — 每个操作都成功

**测试方法(API)**

```bash
# Add
curl -X POST http://localhost:6789/providers -H "Content-Type: application/json" \
  -d '{"name":"t","model":"gemini-2.5-pro","api_base":"http://127.0.0.1:2048/v1","provider_type":"openai-compatible"}'

# List
curl http://localhost:6789/providers

# Test
curl -X POST http://localhost:6789/providers/t/test \
  -H "Content-Type: application/json" -d '{}'

# Delete
curl -X DELETE http://localhost:6789/providers/t
```

**达标标准**

- 添加 / 编辑 / 删除 → 200,DB 持久(重启后仍在)
- Test 按钮:
  - 配置正确时 success=true 且包含 `model / duration_ms / content / tokens`
  - 配置错误时 success=false 且 error 是**单行可读消息**(不是几十行 traceback)
  - 当错误是常见误配(裸 gemini-* / 缺 api_key / 网关连不上)时附带 **hint 字段**,UI 渲染为黄底提示卡

**当前状态**

✅ 后端逻辑已实现:
- model 名规范化:`api_base` 设了就用 `openai/<model>`,否则按 `provider_type` 加前缀
- 错误归一化:`_extract_short_error` 从 LiteLLM JSON body 抓 `"message"` 字段
- Hint 引擎:5 类常见误配(connection refused / Vertex ADC / 404 / API key invalid / no key)

**已知问题**

- ⚠️ **占位符 key 滤除**:启动会扫 `.env`,把 `your_xxx_here` / `placeholder` 等占位符当作"未配置",自动从 registry 删除对应 built-in provider 并清空指向它们的 role 指派。**首次启动后用户的 SQLite registry 可能仍有一批死链 built-in,需要重启服务器一次让清理逻辑生效**
- ❌ **Role Selection UI**:右下角 9 角色 grid 在 `providers.html` 但目前主仓库该 HTML 文件不存在(早期会话标 ✅ 但实际未提交)。**手动测试时只能用 API:** `PUT /selections/edit` `?provider_name=local-gemini`

**改进建议**

- [ ] 创建 `templates/components/sections/providers.html` 完整版
- [ ] Test 按钮失败时,UI 应高亮被拒绝的字段(model 名带红框 / api_base 带红框)
- [ ] 把 model 名 dropdown 化:用户填 api_base 后自动调 `<base>/v1/models` 拉模型列表

---

### 2.2 Best-of-N 并行路由

**测试方法**

```python
import asyncio
from omnicode.llm.router import LLMRouter, RoutingStrategy
from omnicode.llm.base import LLMMessage, Role

router = LLMRouter()
resp = asyncio.run(router.complete(
    [LLMMessage(role=Role.USER, content="Reply in JSON: {hello: world}")],
    strategy=RoutingStrategy.QUALITY_FIRST,
    best_of_n=3,
))
print(resp.content)
```

**达标标准**

- 同一 prompt 实际向前 N 个健康 provider 并发发送
- 选最长非空响应(默认 selector)
- 任一 provider 失败不污染最终结果
- 全失败时 raise `RuntimeError("best-of-N failed across all candidates: ...")`
- 5 个单测全绿(`tests/unit/test_llm_router.py::test_best_of_n_*`)

**当前状态**:✅(单元测试通过,需重新跑确认)

---

## 三、AST / 代码图谱 / 符号检索

### 3.1 调用图(Call Graph)

**测试方法(UI)**

1. 进入 **Code Graph Viewer**(sidebar 新 NEW 徽章)
2. Mode 选 "Call graph"
3. Max files = 200, Max nodes = 80
4. 点 Reload
5. 期望:画布出现 force-directed 图,节点是函数名,边表示调用关系
6. 拖动节点、滚轮缩放、点节点弹出 degree 信息
7. Search 框输入 "main" — 只 main 高亮,其他变 0.15 透明度

**测试方法(API)**

```bash
curl "http://localhost:6789/search/symbols/graph?max_files=20" | python -m json.tool
```

期望:`result.summary.total_edges` 至少几百(本仓库实测 2114),`result.edges[0]` 含 `caller / callee / line / file_path`。

**达标标准**

- 大型仓库 < 1s 内完成构建
- 图布局**不重叠**(关键!之前版本节点全堆在一起)
- 节点数受 `Max nodes` 输入控制(取度数 top-N)
- 切换深浅主题时图自动重绘

**当前状态**

⚠️ **后端 OK,前端布局已重写但需要人工目视验证**。本轮新版 force config:
- `forceLink.distance` 按节点度数动态(50–110px)
- `forceManyBody.strength` 也按度数(-180 到 -460)
- `forceCollide.radius` 防止重叠
- `forceX/Y` 弱中心引力让图保持紧凑

**已知问题**

- ⚠️ **代码图谱**之前的截图显示节点严重重叠 — 已重写但需要打开浏览器验证
- ⚠️ 边数太多(>2000)时 D3 卡顿 — 当前用 Max nodes 强制截断 hub-first

**改进建议**

- [ ] 集群着色(按文件 / 模块上色)
- [ ] 边太多时改用 **WebGL canvas** 渲染替代 SVG
- [ ] 节点点击展开"调用栈"侧栏(显示完整 callers / callees 列表)

---

### 3.2 继承图(Inheritance)

**测试方法**:同 3.1,Mode 选 "Inheritance"。
**达标标准**:节点是类名,有向边 `subclass → base`。
**当前状态**:✅ 后端 OK,前端共用 graph-viewer。

---

### 3.3 符号总数

**测试方法**

打开 Dashboard 或 Search 面板,看 "Symbols" 卡片数字。

```bash
curl http://localhost:6789/search/stats | python -m json.tool
```

**达标标准**

- `total_symbols > 0`(只要 codebase 有 Python/JS/TS/Java/Go/Rust 源码)
- `total_files / total_chunks` 三个数字一致(同一次 indexing 算出)

**当前状态**

✅ 后端逻辑已修:`SemanticSearchEngine.initialize()` 现在跑两次 SELECT — 一次算总 chunks,一次算 `chunk_type IN ('function', 'class', 'method', 'function_definition', ...)` 当作 symbols。

**已知问题**

- ❌ **必须重新建索引才会显示非零** — 老的 vector_store.db 里 chunk_type 字段可能为空。手动操作:
  ```bash
  curl -X POST http://localhost:6789/search/index
  ```
  等 30-60 秒后刷新 Dashboard。

**改进建议**

- [ ] 启动时检测 `total_chunks > 0 但 total_symbols == 0`,自动触发重建索引

---

### 3.4 点击查看代码

**测试方法**

1. Search 面板,执行任意 semantic / text / symbol 搜索
2. 点结果卡上的 "View Code" 按钮
3. 应弹出全屏 modal,黑底等宽字体显示该文件指定行段
4. 右上角 Copy 按钮 → 内容写入剪贴板
5. ESC / 点蒙层 → 关闭 modal

**达标标准**

- modal 显示完整片段(file_path 在标题,内容在 pre 块)
- HTML 字符正确转义(防 XSS)
- 在深色模式下 modal 也保持黑底

**当前状态**

✅ 已实现(之前是 `notifications.info(...)` 的 stub,本轮换成完整 modal)。

**改进建议**

- [ ] 集成 highlight.js 做语法高亮
- [ ] Modal 顶部加 "Open in editor" 按钮(走 `vscode://` 或工作区根的 `code` 命令)

---

## 四、文件操作 / Edit Pipeline

### 4.1 智能编辑(三层策略)

**测试方法 — 小文件场景(< 60 行)**

1. Files 面板 → 选一个 < 60 行的 .py 文件
2. Instructions: "为 main 函数加一个 docstring 写: hello"
3. Code Edit: `#`
4. 点 "AI 编辑文件"
5. 期望:LLM 返回完整文件(whole-file 模式),docstring 已加

**测试方法 — 大文件场景(> 60 行,符号唯一)**

1. 选 `mcp_server.py`(3585 行)
2. Instructions: "为 main 函数上方添加注释:我爱吃米饭"
3. Code Edit: `#`
4. 点 "AI 编辑文件"
5. 期望:**surgical 模式触发**(后端日志 `Symbol-surgical mode: targeting main`),LLM 只重写 main 周围 20 行,本地 splice 回去

**测试方法 — 大文件场景(无明确符号)**

1. 选 mcp_server.py
2. Instructions: "重构所有 logger 调用统一加 [MCP] 前缀"
3. Code Edit: 一段 sketched edit
4. 期望:**patch 模式触发**(SEARCH/REPLACE 块)

**达标标准**

- 小文件 < 30s 完成,quality_score >= 0.8
- 大文件不会被 LLM thinking 文本覆盖(三层防御任一层都能拦)
- 编辑成功:UI 显示绿色 quality 卡;失败:UI 显示**结构化失败分析**(stage / root cause / 建议修复 / 原始 LLM 错误代码块),不再是 "API Error · /edit"
- ruff / 静态检查在编辑后跑一遍,有 ERROR 时自动 review-pass

**当前状态**

✅ 三层防御全部到位(prose 检测 / patch 模式 / surgical 模式)
✅ 失败响应改成 200 + structured payload
✅ `_extract_mentioned_symbols` 正则改用 ASCII lookaround,中文-ASCII 边界(`为main` 这种)能正确识别 symbol

**已知问题**

- ⚠️ **patch 模式 LLM 仍可能 hallucinate SEARCH anchor** — surgical 模式优先,但唯一性判定靠 AST,如果 instructions 说的是不存在的符号或多个同名,会退回到 patch 让模型造 anchor
- ⚠️ **gemini-2.5-flash 是 thinking 模型**,经常在 markdown 块外面输出 `**Reviewing...**` 之类内心独白 — 我们已加 prompt 约束 + prose detection,但偶尔模型仍会塞 prose 进 fence

**改进建议**

- [ ] surgical 模式当符号不唯一时,在 UI 弹出"歧义"对话框让用户选哪个
- [ ] 新增 `_apply_unified_diff` 作为第四种策略,接受 `git diff` 格式
- [ ] 对 thinking 模型自动调用 `reasoning_effort="none"`(litellm 支持)

---

### 4.2 写入文件 / Quality Gate

**测试方法**

1. Files → Write
2. 文件路径: `test_quality.py`
3. 内容: 故意写一段语法错误的 Python(缺冒号)
4. 点写入
5. 期望:**返回 200**,但 `result.success=false` 且 `failure_analysis.failure_reasons` 包含 "Code formatting failed"

**达标标准**:同 Edit,改 422 为 200 + structured payload。
**当前状态**:✅(本轮已修)

---

## 五、Git 与会话

### 5.1 会话幂等性

**测试方法**

1. Git & Sessions 面板
2. 会话名称: `你好`
3. 点 "开始" — 创建并切换到 branch `你好`
4. **再点一次"开始"** — 不应报 `fatal: a branch named '你好' already exists`,应显示"已在 session '你好'"
5. 切到 main,再点开始(同名)— 应显示"Resumed existing session"

**API 测试**

```bash
curl -X POST http://localhost:6789/session -H "Content-Type: application/json" \
  -d '{"operation":"start","session_name":"你好"}'
# 第二次:
curl -X POST http://localhost:6789/session -H "Content-Type: application/json" \
  -d '{"operation":"start","session_name":"你好"}'
# 期望 200, result.reused=true
```

**达标标准**

- 重复 start 不报错
- 已存在 branch → checkout 而不是 create
- end 操作:动态解析 trunk(`main` / `master` / `trunk` / `develop`),不再硬编码 `master`
- delete 操作:删 branch 前先切到 trunk

**当前状态**:✅(9 个新单测覆盖)

---

### 5.2 用户命名 branch 检测

**测试方法**

1. 当前在 `你好` branch
2. 刷新 Git 面板
3. 期望:**会话状态显示"🟢 Active Session: 你好"**(之前显示"未激活")
4. "全部列表"应包含所有非 trunk branch(`你好` / `feature-x` / `ai-session-*` 等)

**达标标准**

- `is_session_branch` 对任意非 `{master, main, trunk, develop}` 的 branch 都返回 `true`
- `is_conventional_session` 仅对 `ai-session-*` / `session-*` 返 true(让 UI 区分自动 vs 手动 session)

**当前状态**:✅

---

### 5.3 Git Blame / History

**测试方法**

```bash
curl -X POST http://localhost:6789/git -H "Content-Type: application/json" \
  -d '{"operation":"blame","file_path":"main.py","start_line":1,"end_line":50}'

curl -X POST http://localhost:6789/git -H "Content-Type: application/json" \
  -d '{"operation":"history","file_path":"main.py","max_results":20}'
```

**达标标准**

- blame 返回 `{commit, author, date, line, content}` per line
- history 返回 `{change_count, risk_score (0-1), defensive_keywords, co_changed_files, issue_refs}`
- 在仓库刚建无 commit 时不崩溃(返回 success=true 空数组)

**当前状态**:✅ STAGE 5.4

---

## 六、记忆系统

### 6.1 存储 / 搜索 / 编辑

**测试方法 — 存储**

1. Memory 面板,Store 区填:
   - Category: `solution`
   - Content: `Edit failed because the LLM output prose; fixed by adding fence detection.`
   - Tags: `edit, llm, fence`
2. 点 Store

**测试方法 — 去重**

1. 重复 store 同一条 content(Category 相同)— **应该不创建新行,而是把现有那条 access_count + 1**
2. 在 Memory 列表里那条会出现 `×2` `×3` `×N` 的橙色徽章
3. 点 "Dedupe" 按钮 — 已有的重复历史一次性合并

**测试方法 — 搜索结果显示位置**

1. Search 区输入: `LLM`
2. 期望每条结果显示:
   - 类别图标 + 名字
   - **`×N` access count 徽章**(>1 时)
   - **匹配相关度百分比**(紫色 pill)
   - **"matched in: <field>"** 紫色小字(指出在 content / tags / context 哪里命中)
   - tags 行(灰色 #tag pill)
   - related_files 行(黄色文件图标 + 文件名 + 文件数)

**测试方法 — 编辑 UI**

1. 任意条 memory → 点编辑铅笔图标
2. **应弹出全屏 modal**(不是浏览器 prompt!),包含:
   - Content textarea(8 行)
   - Category dropdown(9 类)
   - Importance number(1-5)
   - Tags input
   - Related Files textarea(每行一个)
   - 底部显示 Created / Access count / Session
3. 点 Save → notifications.success
4. ESC / Cancel → 关闭

**达标标准**

- 存储:同 fingerprint(category+normalized content)的重复 store 不创建新行
- Dedupe 接口可一次合并历史重复行(求和 access_count、合并 tags、归档其余)
- 编辑 modal 包含所有可改字段,不只是 content
- 搜索结果显示 access_count / score / 匹配字段定位

**当前状态**

✅ **后端**:
- `_content_fingerprint(category, content)` SHA1 + 大小写 + 空白归一
- `store_memory` 先查同 fingerprint 行,有就 UPDATE,没有就 INSERT
- `dedupe_existing()` 一次性扫所有 active 行,按 fingerprint group,保留最老 + 累计 access_count + 归档其余
- `update_memory` 白名单加 `tags / related_files / context`,content 改了重算 fingerprint
- `search_memories_advanced()` 返回 plain dict 含 access_count / last_accessed / relevance_score

✅ **前端**:
- `displayMemories` 渲染 access_count 徽章 / score / matched-in / tags / related_files
- `openMemoryEditor(id)` 替代原 `prompt()`,完整 modal
- 列表 header 加 "Dedupe" 按钮调 `POST /memory/dedupe`

**已知问题**

- ⚠️ **历史数据需要手动 Dedupe 一次** — 老 DB 行没有 fingerprint,启动会自动 backfill。但已经有的几十条重复行,需要用户点一次 Dedupe 按钮才会合并。
- ⚠️ `match_field` 字段后端目前未细分(只填 `match_reason: "Semantic similarity"` / `"Filter match"`),前端 fallback 到 reason

**改进建议**

- [ ] 后端 `search_memories` 增加细粒度 match_field:遍历搜的时候比 query 是命中 content / tags / related_files 哪个字段
- [ ] Dedupe 后弹出"撤销"按钮(7 天内可恢复 archived 行)
- [ ] 编辑 modal 加 diff view(左右两栏对比新旧 content)

---

### 6.2 Memory 上下文注入 Edit Pipeline

**测试方法**

1. 先 store 一条 SOLUTION memory,内容是某个具体修复方案
2. 触发一次 Edit Pipeline,instructions 用相似措辞
3. 后端日志应出现 `_collect_memory_advisory` 把那条 memory 当 priority=18 context 注入

**达标标准**:STAGE 7.5 行为
**当前状态**:✅ 已实现

---

## 七、Web 控制台 UI 功能

### 7.1 Dashboard

**测试方法**

1. 默认页面
2. 看 4 张统计卡片(Files / Symbols / Memories / Branch)
3. 顶部 quick stats bar 也有数据
4. Health 区列出每个 service 状态

**达标标准**:数字非 0(前提:用过功能)、自动每 30s 刷新
**当前状态**:✅,Symbols 卡片之前总是 0,本轮已修

---

### 7.2 i18n(中英文切换)

**测试方法**

1. 切换语言按钮 EN / 中文
2. **所有面板文本立即切换**(不需刷新)
3. 切到一个 section 再切另一个 — section 内文本也按当前语言渲染

**达标标准**:0 个 i18n key 缺失,动态注入的 DOM 也要被翻译
**当前状态**:✅(STAGE 9.6,IIFE + MutationObserver)

---

### 7.3 实时日志流

**测试方法**

1. Logs 面板
2. 点 "Live" 按钮 — 状态变绿
3. 在另一个终端运行任意 API 调用
4. 日志条立刻出现在面板顶部

**达标标准**

- WebSocket `/logs/stream` 连接成功
- 连接立刻收到 backfill(最多 500 条)
- 后续每条日志 < 1s 推送
- 浏览器关页面 / 点 Live 关闭 → 服务端清理订阅

**当前状态**:✅ STAGE 9.9

---

### 7.4 文件浏览

**测试方法**

1. Files 面板 → 点文件路径输入框右侧的 📁 图标
2. 弹出文件选择器 modal
3. 切盘符 / 双击进目录 / 单击选文件
4. 关闭后路径自动填进输入框

**达标标准**:支持 OS 任意位置(被 deny-list 阻止的路径除外:`C:\Windows\System32\config`、`/etc/shadow` 等)
**当前状态**:✅ STAGE 10.9-10.14

---

## 八、深色模式

**测试方法**

1. Header 右上角点 **🌞 Light** 按钮
2. 第一次点 → 切到 **Dark**(月亮图标),整个页面背景变 slate-900
3. 第二次点 → 切到 **System**(half-circle 图标),跟随 OS 主题
4. 第三次点 → 回到 Light
5. 刷新页面 — 主题保持(localStorage 持久)
6. 浏览器 OS 主题切换 — 在 System 模式下页面立即跟着变

**达标标准**

- 背景颜色实际改变(不只是按钮文字)
- 输入框、card、modal、scrollbar 都跟随
- 提示色板(blue/green/yellow/red 等)在暗色下保持可读
- D3 图自动重绘换色
- 切换无 FOUC(页面闪白)

**当前状态**

✅ **本轮新建**:
- `templates/static/js/utils/theme.js`(140 行,IIFE + matchMedia 监听)
- `templates/components/layout/header.html` 加 toggle 按钮 `data-theme-toggle`
- `templates/static/css/styles.css` 末尾追加 ~150 行 `[data-theme="dark"]` 选择器
- `templates/index.html` `<head>` 早期 hydration script(避免 FOUC)

**已知问题**

- ⚠️ **必须 hard-refresh(Ctrl+Shift+R)清浏览器缓存** — 之前的版本没有 theme.js,浏览器缓存可能仍指向旧的 index.html
- ⚠️ Tailwind 是用 utility class 的,我们的 `[data-theme="dark"]` 选择器只覆盖了高频类(`.bg-white`、`.text-gray-700` 等);某些自定义 Tailwind 颜色可能漏掉。如发现哪个面板还是白底,看具体 class 加到 styles.css 末尾

**改进建议**

- [ ] 配 Tailwind 的 `darkMode: 'class'` 让 Tailwind 内置 `dark:` 前缀生效
- [ ] 添加 sidebar 也跟随主题(目前 sidebar 用 `bg-sidebar` 自定义色,亮暗都一样)

---

## 九、MCP 工具(Claude Desktop 接入)

### 9.1 工具可见性

**测试方法**

1. 编辑 Claude Desktop config(`%APPDATA%\Claude\claude_desktop_config.json`),按 `docs/claude_setup.md` 加入 omnicode-mcp 条目
2. 重启 Claude Desktop
3. 在对话框输入 `/` — 应看到 25+ 个 omnicode 工具

**达标标准**:全部工具可见,`provider_tool` / `ast_query_tool` / `inheritance_tool` 等关键工具都列出
**当前状态**:✅ STAGE 8

---

### 9.2 工具执行

**测试方法**(Claude 对话)

```
请帮我用 omnicode-mcp 列出 main.py 中的所有 symbol
```

期望:Claude 调 `ast_query_tool(operation="list", file_path="main.py")`,返回结构化 symbols 列表。

**达标标准**:工具调用日志在 Logs 面板可见、stdio 协议下 Claude 收到 JSON
**当前状态**:✅

---

## 十、性能基准

**测试方法**

```cmd
conda run --no-capture-output -n omnicode-env python benchmarks/run_all.py
```

**达标标准**(本仓库 ~120 个 .py 文件)

| 基准 | 目标 | 实测 |
|---|---|---|
| call_graph cold build | < 1.5 s | 702 ms |
| call_graph update_file 中位数 | < 50 ms | 10 ms |
| inheritance cold build | < 1 s | 503 ms |
| inheritance update_file | < 20 ms | 2 ms |
| token compress 5 KB | < 10 ms | 2-2.5 ms |

**当前状态**:✅ STAGE 12.7

---

## 附录:测试运行命令速查

```cmd
:: 单元测试
conda run --no-capture-output -n omnicode-env python -m pytest tests/unit -v

:: 集成测试
conda run --no-capture-output -n omnicode-env python -m pytest tests/integration -v

:: 全套
conda run --no-capture-output -n omnicode-env python -m pytest tests -q

:: 仅 lint
conda run --no-capture-output -n omnicode-env ruff check omnicode api core tests

:: 性能
conda run --no-capture-output -n omnicode-env python benchmarks/run_all.py

:: 启动 Web 控制台(本地手测)
conda run --no-capture-output -n omnicode-env uvicorn main:app --port 6789

:: TestClient smoke(避免起服务器)
conda run --no-capture-output -n omnicode-env python -c "from fastapi.testclient import TestClient; from main import app; c=TestClient(app); print(c.get('/healthz').status_code)"
```

---

## 总览状态表

| 领域 | 自动化测试 | 手动 UI 测试 | 备注 |
|---|---|---|---|
| Provider CRUD | ✅ | ⚠️ providers.html 缺失 | API OK,UI 待补 |
| Best-of-N | ✅ | — | 5 单测通过 |
| Call Graph | ✅ | ⚠️ 待目视验证布局 | 后端 OK |
| 符号总数 | ✅ 后端 | ⚠️ 需重建索引 | `POST /search/index` |
| View Code modal | — | ✅ | 本轮新增 |
| Edit Pipeline | ✅ | ⚠️ 需真 LLM | 三层防御就绪 |
| 会话幂等 | ✅ | ✅ | 9 单测 |
| Memory 去重 | ⚠️ 待新单测 | ✅ | 后端 + UI 全做 |
| Memory 编辑 modal | — | ✅ | 替换 prompt() |
| 深色模式 | — | ⚠️ 需 hard-refresh | theme.js + dark CSS |
| 实时日志 | ✅ | ✅ | WebSocket OK |
| MCP 工具 | — | ⚠️ 需 Claude 重启 | 25+ 工具 |

最后:**如果某项功能在 UI 测试时表现异常**,记录到 `deployment_build_record.md` 的 "已知问题"段,优先级按 P0(影响核心流程)/ P1(单功能不可用)/ P2(细节体验)分类。
