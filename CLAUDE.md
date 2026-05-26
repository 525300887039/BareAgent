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

### 用户界面 (`src/ui/`)
`AgentConsole`（基于 rich 的输出）、`StreamPrinter`（流式输出）、`prompt.py`（基于 prompt-toolkit 的输入层）、`theme.py`（主题，默认 `catppuccin-mocha`）。后台通知通过 `concurrency/notification.py` 实现。

### 后台执行 (`src/concurrency/`)
`BackgroundManager`（background.py）：基于 threading 的后台任务管理，支持 submit/drain_notifications。`NotificationManager`（notification.py）：后台任务完成通知。

### 会话管理
`TranscriptManager`（memory/transcript.py）：会话转录持久化。REPL 支持 `/sessions` 列出历史会话、`/resume` 恢复会话、`/new` 开始新会话、`/clear` 清屏并重置。每个会话有唯一 ID（时间戳格式）。

### 追踪 (`src/tracing/`)
统一 Tracer 接口（`_api.py`）+ 代理（`_proxy.py`）+ 配置入口（`setup.py`）。后端：`JsonFileTracer`（始终启用，写入 `.logs/` 供 `/log` 与 web viewer 使用）、`LangfuseTracer`（设 `LANGFUSE_PUBLIC_KEY` 或 `[tracing] langfuse=true` 启用）、`OpenTelemetryTracer`（设 `OTEL_EXPORTER_OTLP_ENDPOINT` 或 `[tracing] opentelemetry=true` 启用）。多后端时通过 `CompositeTracer` 扇出。Langfuse/OTel 为可选依赖，需安装额外 extras。

### 调试与日志 (`src/debug/`)
`InteractionLogger`（interaction_log.py）：将完整 LLM 请求/响应 payload 按会话写入 `.logs/<session-id>/` 的 JSONL，支持订阅事件流。`DebugViewerHandler`（web_viewer.py + viewer.html）：内置只读 HTTP SPA，REPL 中通过 `/log` 命令启动（端口由 `[debug] viewer_port` 控制，默认 8321）。需在配置中将 `[debug] enabled` 设为 `true` 才会写日志。

## 配置

`config.toml`（默认值）→ `config.local.toml`（本地覆盖，已 git-ignore）→ 环境变量 / CLI 参数（优先级递增）。

关键环境变量：`BAREAGENT_CONFIG`、`BAREAGENT_PROVIDER`、`BAREAGENT_MODEL`、`BAREAGENT_API_KEY_ENV`、`BAREAGENT_BASE_URL`、`BAREAGENT_PERMISSION_MODE`、`BAREAGENT_UI_STREAM`、`BAREAGENT_UI_THEME`、`BAREAGENT_THINKING_MODE`、`BAREAGENT_THINKING_BUDGET_TOKENS`、`BAREAGENT_SKILLS_DIR`、`BAREAGENT_SUBAGENT_MAX_DEPTH`、`BAREAGENT_SUBAGENT_DEFAULT_TYPE`。追踪相关：`LANGFUSE_PUBLIC_KEY`、`OTEL_EXPORTER_OTLP_ENDPOINT`。

CLI 参数：`--provider`、`--model`、`--config`。

## 代码规范

- 优先使用 Python 3.12+ 特性和标准库
- 遵循 PEP 8，保持清晰的类型注解
- 提交信息遵循 Conventional Commits：`Fix:`、`Feat:`、`Refactor:`、`Test:`、`Docs:`
- 新增行为需在 `tests/` 中补充 pytest 测试
- 保持实现简洁，避免过度设计
