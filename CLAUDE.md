# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

BareAgent 是一个纯 Python 终端代码智能体，支持可插拔 LLM 提供商、细粒度权限控制、多智能体协调和可扩展技能系统。基于 Python 3.12+，使用 Hatchling 构建。

## 常用命令

```bash
# 安装（可编辑模式）
uv pip install -e ".[dev]"
# 可选追踪后端
uv pip install -e ".[langfuse]"        # Langfuse
uv pip install -e ".[otel]"            # OpenTelemetry
uv pip install -e ".[all-tracing]"     # 全部

# 运行
bareagent                          # 或: python -m src.main
bareagent --provider anthropic --model claude-sonnet-4-20250514
bareagent --config ~/my_config.toml

# 测试
pytest                             # 全部测试
pytest tests/test_loop.py          # 单个文件
pytest tests/test_loop.py -k "test_name"  # 单个测试

# 代码检查与格式化
ruff check src tests               # 检查
ruff check --fix src tests          # 自动修复
ruff format src tests               # 格式化
```

## 架构

### 核心智能体循环 (`src/core/loop.py`)
`agent_loop()` 是中央调度器：调用 LLM → 解析工具调用 → 权限检查 → 执行处理器 → 收集结果。最多迭代 `max_iterations`（200）次。支持流式输出和长对话消息压缩。

### 提供商抽象 (`src/provider/`)
`BaseLLMProvider`（base.py）为抽象基类，`AnthropicProvider` 和 `OpenAIProvider` 为具体实现（OpenAI provider 也覆盖 DeepSeek 等 OpenAI 兼容端点）。`factory.py` 负责工厂创建。统一的 `LLMResponse` 包含工具调用、文本、思考过程、token 计数。支持流式（`create_stream()`）和非流式（`create()`）。

### 工具系统 (`src/core/tools.py`)
工具以可调用对象注册在字典中。基础工具（`BASE_TOOLS`）：bash、read_file、write_file、edit_file、glob、grep、web_fetch、web_search。延迟加载工具（`DEFERRED_TOOLS`）：todo_*、task_*、subagent、load_skill、background_run、team_*。Schema 定义在 `core/schema.py`，处理器在 `core/handlers/`（含 `web_fetch.py`、`web_search.py`、`search_utils.py`）。

### 权限模型 (`src/permission/guard.py`)
模式：DEFAULT（写操作需确认）、AUTO（安全模式自动批准）、PLAN（仅允许安全工具）、BYPASS（无检查）。内置危险模式检测（rm -rf、force push、DROP TABLE 等）。支持 allow/deny 规则（前缀匹配）。`clone()` 创建权限副本，`for_subagent()` 为子智能体创建隔离权限（模式级联 + fail-closed）。运行时可通过 `/default`、`/auto`、`/plan`、`/bypass`、`/mode` 命令或 `Shift+Tab` 快捷键切换权限模式。

### 多智能体协调 (`src/team/`)
`MessageBus`（基于 JSONL 的追加式邮箱）、`ProtocolFSM`（带轮询的请求-响应协议）、`AutonomousAgent`（守护进程式空闲-轮询-认领循环）、`TeammateManager`。协议：PLAN_APPROVAL、SHUTDOWN。

### 智能体类型系统 (`src/planning/agent_types.py`)
`AgentType` 冻结数据类定义子智能体配置（工具白/黑名单、max_turns、嵌套控制、权限模式覆盖）。内置四种类型：`general-purpose`（全量工具，可嵌套，200 轮）、`explore`（只读，50 轮）、`plan`（只读，50 轮）、`code-review`（只读，50 轮）。`resolve_agent_type()` 解析类型名称并回退到默认值。`filter_tools()` / `filter_handlers()` 按类型过滤工具和处理器。

### 子智能体委派 (`src/planning/subagent.py`)
隔离的消息上下文，递归深度限制（max_depth=3），基于 token 的消息压缩（50k 阈值）。支持 `agent_type` 参数选择智能体类型，`run_in_background` 参数后台异步执行。权限隔离：通过 `PermissionGuard.for_subagent()` 创建子级权限，后台智能体使用 fail-closed 模式。

### 技能系统 (`src/planning/skills.py`)
从 `skills/*/SKILL.md` 自动发现技能。通过 `load_skill` 工具按需加载。当前技能：code-review、git、test。

### 任务与 TODO 管理 (`src/planning/`)
`TaskManager`（tasks.py）：持久化 JSON 存储，状态（pending/in_progress/done/failed），依赖追踪。`TodoManager`（todo.py）：内存级会话作用域，优先级，提醒机制。

### 消息压缩 (`src/memory/compact.py`)
微压缩截断旧工具结果；完整压缩通过 LLM 生成摘要。基于阈值触发（50k tokens）。保留系统消息和近期上下文。

### 持久化记忆 (`src/memory/persistent.py`)
文件式跨会话记忆：一条记忆 = 一个带 frontmatter（name/description/metadata.type，type ∈ user/feedback/project/reference）的 `.md` 文件，`MEMORY.md` 作单行索引。`MemoryManager` 暴露六个文本编辑器式命令（`view`/`create`/`str_replace`/`insert`/`delete`/`rename`），契约对齐 Anthropic memory tool，但注册为**单个普通 client tool** `memory`（schema 在 `core/tools.py:MEMORY_TOOL_SCHEMAS`，handler `core/handlers/memory.py:run_memory` 薄封装委派给 manager），不绑定原生 `memory_20250818` 类型，故全 provider 通用。所有路径经 `core/sandbox.py:safe_path` 限制在记忆根内（兼容 strip `/memories/` 前缀），写入走 `fileutil.atomic_write_text`。存储位置可配置 `[memory] dir`，默认全局 `~/.bareagent/projects/<workspace-slug>/memory/`（slug 由 `derive_memory_slug` 派生）。会话开局 `MemoryManager.system_prompt_section()` 把 MEMORY.md 索引（前 `max_index_lines` 行）+ MEMORY PROTOCOL 注入 `assemble_system_prompt`。**召回层（recall layer，仿 Claude Code 相关性召回）**：`MemoryManager.recall(query, k)` / `recall_section(query, k)` 按 frontmatter `name + description`（缺失回退正文前 200 字）做跨语言词法匹配（ASCII 词 + 中文滑动 bigram），取 top-K 拼成 `<memory-recall>` 块；`main.py:_refresh_memory_recall`（仿 `_refresh_nag_reminder`，在 `_build_loop_compact` 的 `_compact` 里每轮 agent_loop 调用）剔除旧块并在最后一条真实 user 消息后注入新块，故 `/remember`、`/forget`、普通 user-turn 均自动获得逐轮召回注入，与开局索引注入互补。`recall_k`（`[memory] recall_k`，默认 5，0 = 关闭召回仅保留索引注入）控制条数。向量/语义召回仍是 `system_prompt_section()` 与 `recall()` 的后续升级位。权限：`memory` 入 `PermissionGuard.SAFE_TOOLS`（不弹确认，沙箱内 bookkeeping）。子代理只读隔离：`AgentType.memory_writable`（explore/plan/code-review 默认 False）——单工具无法按子命令名过滤，故由 `subagent.py:_make_readonly_memory_handler` 在子代理边界包装 handler、拒绝五个写命令、放行 `view`。REPL 命令：`/remember <文本>`、`/forget <文本>`（注入用户指令驱动 LLM 经工具落盘/删除并维护索引）。配置见 `config.toml [memory]`。

### 用户界面 (`src/ui/`)
`AgentConsole`（基于 rich 的输出）、`StreamPrinter`（流式输出）、`prompt.py`（基于 prompt-toolkit 的输入层）、`theme.py`（主题，默认 `catppuccin-mocha`）。后台通知通过 `concurrency/notification.py` 实现。

### 后台执行 (`src/concurrency/`)
`BackgroundManager`（background.py）：基于 threading 的后台任务管理，支持 submit/drain_notifications。`NotificationManager`（notification.py）：后台任务完成通知。

### 会话管理
`TranscriptManager`（memory/transcript.py）：会话转录持久化。REPL 支持 `/sessions` 列出历史会话、`/resume` 恢复会话、`/new` 开始新会话、`/clear` 清屏并重置。每个会话有唯一 ID（时间戳格式）。

### Token 用量与成本 (`src/memory/token_tracker.py`)
`TokenTracker`：进程级累计 LLM token 用量（`total_input`/`total_output`/`call_count` + 按 model 细分），`record(response, model)` 在 `agent_loop`（`loop.py`）每次 LLM 响应后调用（流式+非流式单点覆盖，可选 `token_tracker` 参数）。REPL `/cost` 命令展示当前会话累计：**总是**显 token 计数 + per-model 细分，有定价的 model 额外显 $ 估算，无价的标 `(no price)`。定价为**混合策略**：内置 Claude Opus/Sonnet/Haiku 4.x 参考价（`DEFAULT_PRICES`，前缀匹配，价格可能漂移），`[cost.prices."<model-id>"]`（单位每百万 token）可覆盖内置价或为任意 model 新增价；未知且未配价的 model 只显 token 不显 $。重置语义：`/new`·`/clear`·`/resume` 归零，`/compact` 不重置（同会话压缩）。配置见 `config.toml [cost]`。

### 追踪 (`src/tracing/`)
统一 Tracer 接口（`_api.py`）+ 代理（`_proxy.py`）+ 配置入口（`setup.py`）。后端：`JsonFileTracer`（始终启用，写入 `.logs/` 供 `/log` 与 web viewer 使用）、`LangfuseTracer`（设 `LANGFUSE_PUBLIC_KEY` 或 `[tracing] langfuse=true` 启用）、`OpenTelemetryTracer`（设 `OTEL_EXPORTER_OTLP_ENDPOINT` 或 `[tracing] opentelemetry=true` 启用）。多后端时通过 `CompositeTracer` 扇出。Langfuse/OTel 为可选依赖，需安装额外 extras。

### 调试与日志 (`src/debug/`)
`InteractionLogger`（interaction_log.py）：将完整 LLM 请求/响应 payload 按会话写入 `.logs/<session-id>/` 的 JSONL，支持订阅事件流。`DebugViewerHandler`（web_viewer.py + viewer.html）：内置只读 HTTP SPA，REPL 中通过 `/log` 命令启动（端口由 `[debug] viewer_port` 控制，默认 8321）。需在配置中将 `[debug] enabled` 设为 `true` 才会写日志。

### MCP 客户端 (`src/mcp/`)
将外部 [Model Context Protocol](https://modelcontextprotocol.io) server 作为可插拔工具源接入 BareAgent。`MCPManager`（manager.py）并发拉起所有 `[[mcp.servers]]`，每个 server 一个 `MCPClient`（client.py）+ `Transport`（transport/，ABC + stdio / http_legacy / http_streamable 三实现）。`registry.py` 把远端工具按 `mcp__<server>__<tool>` 命名注入 `get_tools()` / `get_handlers()`；声明 `resources` capability 的 server 额外得到 `mcp__<server>__resource_list` + `mcp__<server>__resource_read`；`prompts/list` 通过 REPL slash 命令 `/mcp:<server>:<prompt>` 触发。REPL 配套命令：`/mcp status|list|reload`。生命周期硬化：transport reader 线程主动感知 EOF / 断流 → manager 立刻标 UNHEALTHY 并通过 `BackgroundManager.notify` 推送通知；`atexit + SIGTERM` 兜底清理子进程；单次 tool result 在 registry 层按 `max_result_text_bytes` / `max_result_binary_bytes` 截断（256 KiB / 5 MiB 默认）以保护 LLM 上下文。子代理隔离：`AgentType.mcp_tools_enabled`（explore/plan/code-review 默认 False）。关键文件：`src/mcp/__init__.py`、`src/mcp/manager.py`、`src/mcp/registry.py`、`src/mcp/client.py`、`src/mcp/transport/`、`src/mcp/config.py`、`src/mcp/errors.py`。配置见 `config.toml [mcp]` + `[[mcp.servers]]`。

### LSP 客户端 (`src/lsp/`)
通过 [multilspy](https://github.com/microsoft/multilspy)（可选 extra：`uv pip install -e ".[lsp]"`）接入成熟 Language Server，让 LLM 拿到精确的符号导航 + 类型诊断。**multilspy 0.0.15 的语言到 server 映射**：Python → `jedi-language-server`（非 pyright；jedi 适合符号/导航，类型诊断弱）；TypeScript → `typescript-language-server`；Rust → `rust-analyzer`。`LanguageServerManager`（manager.py）按 `[[lsp.servers]]` 并发拉起所有 server，按文件扩展名路由；handshake 失败 / 超时标 UNHEALTHY 不阻塞 REPL boot。`tools.py` 注入四个只读 Tier-1 查询工具到 `DEFERRED_TOOLS`：`lsp_outline` / `lsp_definition` / `lsp_references` / `lsp_diagnostics`（坐标对 LLM 暴露 1-based，内部转 0-based 调 LSP）。**写工具 `semantic_rename(file, line, col, new_name)`**（引用感知的语义重命名，基于 `textDocument/rename`）**故意不带 `lsp_` 前缀**——`lsp_*`=只读查询、`semantic_rename`=写盘，读写边界干净。multilspy 0.0.15 的 `SyncLanguageServer` 无 rename 同步包装，`LanguageServerManager.request_rename` 走裸请求：`asyncio.run_coroutine_threadsafe(server.language_server.server.send.rename(params), sync_server.loop)` + `open_file` didOpen。`src/lsp/workspace_edit.py`（纯函数）解析 WorkspaceEdit 的 `changes` / `documentChanges` 两种形态、按 uri 分组、单文件内按位置倒序应用 TextEdit、`atomic_write_text` 落盘；资源型操作（CreateFile/RenameFile/DeleteFile）MVP 安全跳过并提示（不做文件级重命名）。语义：LSP 不可用 / 无路由 / 空编辑 → 显式 Error 不静默退化为文本替换（无 grep 回退、无 dry-run、无 prepareRename 预校验）。权限：`semantic_rename` 不入 `SAFE_TOOLS`，与 `write_file` 同档——DEFAULT 确认 / AUTO 通过 / PLAN 拒绝 / BYPASS 放行。子代理隔离：因不带 `lsp_` 前缀，加入 `agent_types._READ_ONLY_DEFAULTS["disallowed_tools"]`，explore/plan/code-review 拿不到该写工具。`diagnostics.py` 提供 Hybrid auto-diagnostics-on-edit 钩子（默认 OFF；`[lsp] auto_diagnostics_on_edit = true` 开启后，`edit_file` / `write_file` 成功后通过 diff 算法五元组 `(file, line, col, severity, message)` 计算新增诊断并追加 `Newly introduced diagnostics in <file>:` 段到 tool result）。生命周期硬化：multilspy 0.0.15 默认 `do_nothing` 吃掉所有 `publishDiagnostics`，manager handshake 后直接覆盖 `language_server.server.on_notification_handlers["textDocument/publishDiagnostics"]`，缓存到 `_ServerEntry.diagnostics`；watchdog 线程轮询 subprocess `returncode` 检测崩溃 → 标 UNHEALTHY + console 推送 + `BackgroundManager.notify(f"lsp:{language}", ...)`；`atexit` 注册 `close_all`（幂等，与 MCP 的 atexit 解耦共存）。REPL 命令：`/lsp status|list|reload <language>`。子代理隔离：`AgentType.lsp_tools_enabled`（4 个查询工具只读，explore/plan/code-review 默认 True）；写工具 `semantic_rename` 另由 `disallowed_tools` 黑名单隔离。关键文件：`src/lsp/{__init__,manager,tools,workspace_edit,config,diagnostics,coord,errors}.py`。配置见 `config.toml [lsp]` + `[[lsp.servers]]`（`semantic_rename` 无新增配置项，沿用现有 LSP 配置）。

## 配置

`config.toml`（默认值）→ `config.local.toml`（本地覆盖，已 git-ignore）→ 环境变量 / CLI 参数（优先级递增）。

关键环境变量：`BAREAGENT_CONFIG`、`BAREAGENT_PROVIDER`、`BAREAGENT_MODEL`、`BAREAGENT_API_KEY`、`BAREAGENT_API_KEY_ENV`、`BAREAGENT_BASE_URL`、`BAREAGENT_PERMISSION_MODE`、`BAREAGENT_UI_STREAM`、`BAREAGENT_UI_THEME`、`BAREAGENT_THINKING_MODE`、`BAREAGENT_THINKING_BUDGET_TOKENS`、`BAREAGENT_SKILLS_DIR`、`BAREAGENT_SUBAGENT_MAX_DEPTH`、`BAREAGENT_SUBAGENT_DEFAULT_TYPE`。追踪相关：`LANGFUSE_PUBLIC_KEY`、`OTEL_EXPORTER_OTLP_ENDPOINT`。

CLI 参数：`--provider`、`--model`、`--config`。

## 代码规范

- 优先使用 Python 3.12+ 特性和标准库
- 遵循 PEP 8，保持清晰的类型注解
- 提交信息遵循 Conventional Commits：`Fix:`、`Feat:`、`Refactor:`、`Test:`、`Docs:`
- 新增行为需在 `tests/` 中补充 pytest 测试
- 保持实现简洁，避免过度设计

详细工程约定见 `.trellis/spec/backend/`（trellis 自动注入到所有 sub-agent 上下文）。
