# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指引。

## 项目概览

BareAgent 是一个纯 Python 终端代码智能体，支持可插拔 LLM 提供商、细粒度权限控制、多智能体协调和可扩展技能系统。基于 Python 3.12+，使用 Hatchling 构建。

## 常用命令

```bash
# 安装（可编辑模式）
uv pip install -e ".[dev]"

# 运行
bareagent                          # 或: python -m src.main

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
`BaseLLMProvider`（base.py）为抽象基类，`AnthropicProvider` 和 `OpenAIProvider` 为具体实现。`factory.py` 负责工厂创建。统一的 `LLMResponse` 包含工具调用、文本、思考过程、token 计数。支持流式（`create_stream()`）和非流式（`create()`）。

### 工具系统 (`src/core/tools.py`)
工具以可调用对象注册在字典中。基础工具：bash、read_file、write_file、edit_file、glob、grep。延迟加载工具：todo_*、task_*、subagent、load_skill、background_run、team_*。Schema 定义在 `core/schema.py`，处理器在 `core/handlers/`。

### 权限模型 (`src/permission/guard.py`)
模式：DEFAULT（写操作需确认）、AUTO（安全模式自动批准）、PLAN（仅允许安全工具）、BYPASS（无检查）。内置危险模式检测（rm -rf、force push、DROP TABLE 等）。后台智能体使用 fail-closed 模式。

### 多智能体协调 (`src/team/`)
`MessageBus`（基于 JSONL 的追加式邮箱）、`ProtocolFSM`（带轮询的请求-响应协议）、`AutonomousAgent`（守护进程式空闲-轮询-认领循环）、`TeammateManager`。协议：PLAN_APPROVAL、SHUTDOWN。

### 子智能体委派 (`src/planning/subagent.py`)
隔离的消息上下文，递归深度限制（max_depth=3），消息压缩。继承父级的工具和处理器。

### 技能系统 (`src/planning/skills.py`)
从 `skills/*/SKILL.md` 自动发现技能。通过 `load_skill` 工具按需加载。当前技能：code-review、git、test。

### 任务与 TODO 管理 (`src/planning/`)
`TaskManager`（tasks.py）：持久化 JSON 存储，状态（pending/in_progress/done/failed），依赖追踪。`TodoManager`（todo.py）：内存级会话作用域，优先级，提醒机制。

### 消息压缩 (`src/memory/compact.py`)
微压缩截断旧工具结果；完整压缩通过 LLM 生成摘要。基于阈值触发（50k tokens）。保留系统消息和近期上下文。

### 用户界面 (`src/ui/`)
`AgentConsole`（基于 rich 的输出）、`StreamPrinter`（流式输出）。后台通知通过 `concurrency/notification.py` 实现。

## 配置

`config.toml`（默认值）→ `config.local.toml`（本地覆盖，已 git-ignore）→ 环境变量。

关键环境变量：`BAREAGENT_CONFIG`、`BAREAGENT_PROVIDER`、`BAREAGENT_MODEL`、`BAREAGENT_API_KEY_ENV`、`BAREAGENT_PERMISSION_MODE`、`BAREAGENT_UI_STREAM`、`BAREAGENT_THINKING_MODE`、`BAREAGENT_THINKING_BUDGET_TOKENS`、`BAREAGENT_SKILLS_DIR`。

## 代码规范

- 优先使用 Python 3.12+ 特性和标准库
- 遵循 PEP 8，保持清晰的类型注解
- 提交信息遵循 Conventional Commits：`Fix:`、`Feat:`、`Refactor:`、`Test:`、`Docs:`
- 新增行为需在 `tests/` 中补充 pytest 测试
- 保持实现简洁，避免过度设计
