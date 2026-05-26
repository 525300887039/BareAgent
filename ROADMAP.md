# BareAgent 开发路线图

> 基于当前 26 次提交的实现状态，对照 Claude Code 设计，梳理出的后续开发计划。
> 生成日期：2026-04-19

---

## 当前已实现模块一览

| 模块 | 状态 | 核心文件 |
|------|------|----------|
| 智能体循环 | 已完成 | `src/core/loop.py` |
| 提供商抽象（Anthropic / OpenAI） | 已完成 | `src/provider/` |
| 流式输出 | 已完成 | `src/ui/stream.py` |
| 基础工具（bash/read/write/edit/glob/grep） | 已完成 | `src/core/handlers/` |
| 四级权限模型 + 危险命令检测 + allow/deny 规则 | 已完成 | `src/permission/` |
| 子智能体委派（类型系统、递归深度、权限隔离） | 已完成 | `src/planning/subagent.py` |
| 多智能体协调（MessageBus / ProtocolFSM / AutonomousAgent） | 已完成 | `src/team/` |
| 消息压缩（微压缩 + LLM 摘要） | 已完成 | `src/memory/compact.py` |
| 会话管理（transcript / resume / sessions） | 已完成 | `src/memory/transcript.py` |
| 技能系统（SKILL.md 自动发现 + 按需加载） | 已完成 | `src/planning/skills.py` |
| 后台执行 | 已完成 | `src/concurrency/` |
| 任务 & TODO 管理 | 已完成 | `src/planning/tasks.py`, `todo.py` |
| 调试日志 & Web Viewer | 已完成 | `src/debug/` |
| Tracing（Langfuse / OpenTelemetry） | 已完成 | `src/tracing/` |
| 主题系统 | 已完成 | `src/ui/theme.py` |
| prompt-toolkit 终端交互 | 已完成 | `src/ui/prompt.py` |
| 路径沙箱 | 已完成 | `src/core/sandbox.py` |

---

## 第一阶段：补全工具层能力

> 目标：让智能体能连接外部世界，从"本地文件操作"升级为"全能助手"。
> 建议耗时：每个功能 1-2 周

### 1.1 MCP (Model Context Protocol) 客户端

**优先级：最高** — 这是整个扩展体系的基石，实现后其他外部工具都可以通过 MCP 接入。

**Claude Code 中的设计：**
- 用户在 settings.json 中声明 MCP 服务器（命令行 + 环境变量）
- 启动时自动拉起 MCP 子进程，通过 stdio 进行 JSON-RPC 通信
- 握手后发现服务器提供的工具 schema，动态注入到智能体的工具列表
- 工具调用时透明转发给对应的 MCP 服务器
- 支持 deferred tools（延迟加载 schema，按需获取）

**需要实现的组件：**

```
src/mcp/
├── __init__.py
├── client.py          # MCP 客户端：子进程管理、JSON-RPC 通信
├── registry.py        # 工具注册表：发现 → schema 转换 → 注入 tools 列表
├── transport.py       # 传输层：stdio pipe 读写、消息帧解析
└── config.py          # 配置解析：从 config.toml 读取 MCP 服务器声明
```

**关键学习点：**
- JSON-RPC 2.0 协议（request/response/notification）
- 子进程生命周期管理（启动、健康检查、优雅关闭、崩溃重启）
- 工具 schema 的动态注册与卸载
- 并发：多个 MCP 服务器同时运行，工具调用路由

**实现步骤：**
1. 实现 stdio transport（读写 JSON-RPC 消息）
2. 实现 MCP 握手流程（initialize → initialized）
3. 实现 tools/list 调用，将远程工具 schema 转换为本地格式
4. 在 `get_tools()` 和 `get_handlers()` 中注入 MCP 工具
5. 实现工具调用转发（tools/call）
6. 配置解析：支持在 config.toml 中声明 MCP 服务器
7. 生命周期管理：启动、关闭、错误处理

**配置示例：**
```toml
[[mcp.servers]]
name = "context7"
command = "npx"
args = ["-y", "@context7/mcp-server"]

[[mcp.servers]]
name = "fetch"
command = "uvx"
args = ["mcp-server-fetch"]
```

---

### 1.2 Web 工具（WebFetch / WebSearch）

**优先级：高** — 当前智能体完全无法获取互联网信息。

**Claude Code 中的设计：**
- `WebFetch`：获取指定 URL 的内容，自动将 HTML 转为可读文本
- `WebSearch`：调用搜索引擎 API，返回结构化搜索结果

**需要实现的组件：**

```
src/core/handlers/
├── web_fetch.py       # URL 内容获取 + HTML-to-text
└── web_search.py      # 搜索引擎 API 封装
```

**关键学习点：**
- HTTP 客户端（httpx，已在依赖中）
- HTML 转纯文本（可用 `html.parser` 标准库，或引入 `beautifulsoup4`）
- 内容截断策略（LLM 上下文有限，需要智能截断）
- 搜索 API 对接（可选：Brave Search API、SerpAPI、或自建 DuckDuckGo 抓取）

**实现步骤：**
1. 实现 `web_fetch` handler：httpx GET → 检测 content-type → HTML 转文本 → 截断
2. 注册工具 schema 到 `TOOL_SCHEMAS`
3. 实现 `web_search` handler：调用搜索 API → 解析结果 → 格式化输出
4. 添加超时、重试、User-Agent 等基础 HTTP 配置
5. 权限集成：web 工具应该需要确认（DEFAULT 模式下）

**工具 schema 参考：**
```python
_schema("web_fetch", "Fetch content from a URL.", {
    "url": {"type": "string", "description": "URL to fetch."},
    "max_length": {"type": "integer", "description": "Max chars to return.", "default": 10000},
}, ["url"])

_schema("web_search", "Search the web.", {
    "query": {"type": "string", "description": "Search query."},
    "max_results": {"type": "integer", "description": "Max results.", "default": 5},
}, ["query"])
```

---

### 1.3 多模态文件读取（图片 / PDF）

**优先级：中** — 让智能体能"看"图片和读 PDF。

**Claude Code 中的设计：**
- 读取图片文件时，将内容编码为 base64，作为 image content block 发送给 LLM
- 读取 PDF 时，提取文本内容（支持分页读取）
- 读取 Jupyter notebook 时，解析 cells 和 outputs

**需要修改的文件：**
- `src/core/handlers/file_read.py` — 扩展 `run_read` 支持二进制文件检测和多模态处理

**实现步骤：**
1. 在 `run_read` 中检测文件扩展名（.png/.jpg/.gif/.webp/.pdf/.ipynb）
2. 图片：读取二进制 → base64 编码 → 返回特殊格式让 loop 构造 image content block
3. PDF：引入 `pymupdf` 或 `pdfplumber` → 提取指定页范围的文本
4. Notebook：解析 JSON 结构 → 提取 code cells 和 markdown cells
5. 修改 `agent_loop` 中的 `_tool_result` 以支持多模态内容块

**注意事项：**
- 需要修改 `_tool_result()` 函数，支持返回 list 类型的 content（包含 image block）
- 图片大小限制（建议 < 5MB，超过则缩放）
- PDF 分页读取（避免一次性加载整个大文件）

---

## 第二阶段：提升智能体质量

> 目标：从"能用"到"好用"，提升交互体验和智能程度。
> 建议耗时：每个功能 1 周左右

### 2.1 Hooks 系统（工具调用前后的用户自定义钩子）

**优先级：高** — 这是让用户自定义智能体行为的核心机制。

**Claude Code 中的设计：**
- 用户在 settings.json 中配置 hooks：指定触发事件 + shell 命令
- 事件类型：`PreToolUse`（工具调用前）、`PostToolUse`（工具调用后）、`Notification`、`Stop` 等
- hook 可以阻止工具执行（返回非零退出码）、修改工具输入、或注入额外信息
- hook 的 stdin 接收 JSON 格式的事件上下文

**需要实现的组件：**

```
src/hooks/
├── __init__.py
├── engine.py          # Hook 引擎：匹配事件 → 执行 shell 命令 → 处理结果
├── config.py          # Hook 配置解析
└── events.py          # 事件类型定义
```

**关键学习点：**
- 事件驱动架构（发布-订阅模式）
- 子进程通信（stdin 传入 JSON 上下文，stdout/stderr 捕获输出）
- 拦截器模式（hook 可以阻止或修改工具调用）

**实现步骤：**
1. 定义事件类型枚举（PreToolUse / PostToolUse / Stop / Notification）
2. 实现 hook 配置解析（从 config.toml 或 settings.json 读取）
3. 实现 hook 执行引擎：匹配事件 → 构造 JSON 上下文 → 执行 shell 命令 → 解析结果
4. 在 `agent_loop` 的工具执行前后插入 hook 调度点
5. 处理 hook 返回值：阻止执行、修改输入、注入消息

**配置示例：**
```toml
[[hooks]]
event = "PreToolUse"
tool = "bash"
command = "echo 'About to run bash command' >&2"

[[hooks]]
event = "PostToolUse"
tool = "write_file"
command = "ruff format $FILE_PATH"
```

---

### 2.2 持久化记忆系统

**优先级：高** — 让智能体跨会话记住用户偏好和项目上下文。

**Claude Code 中的设计：**
- 基于文件的记忆系统，存储在 `~/.claude/projects/<project>/memory/`
- 记忆类型：user（用户画像）、feedback（行为反馈）、project（项目上下文）、reference（外部引用）
- MEMORY.md 作为索引文件，每条记忆一个独立 .md 文件（带 frontmatter）
- 智能体在每次对话开始时加载相关记忆，对话中按需保存

**需要实现的组件：**

```
src/memory/
├── persistent.py      # 持久化记忆管理器（读写 .md 文件）
├── index.py           # MEMORY.md 索引管理
└── types.py           # 记忆类型定义（user/feedback/project/reference）
```

**关键学习点：**
- 知识管理系统设计
- Frontmatter 解析（YAML header in markdown）
- 记忆检索策略（按类型、按相关性）
- 记忆生命周期（创建、更新、过期、删除）

**实现步骤：**
1. 定义记忆数据模型（name, description, type, content）
2. 实现 .md 文件读写（带 YAML frontmatter）
3. 实现 MEMORY.md 索引的自动维护
4. 添加 `memory_read` / `memory_write` 工具供智能体调用
5. 在 `assemble_system_prompt` 中注入相关记忆
6. 在 REPL 中支持 `/remember` 和 `/forget` 命令

**文件结构示例：**
```
~/.bareagent/memory/
├── MEMORY.md                    # 索引
├── user_role.md                 # 用户是高级 Python 开发者
├── feedback_no_summary.md       # 不要在回复末尾做总结
└── project_auth_rewrite.md      # 正在重写认证模块
```

---

### 2.3 Token 用量追踪与成本展示

**优先级：中** — 简单但实用。

**当前状态：** `LLMResponse` 已有 `input_tokens` / `output_tokens`，但没有累计和展示。

**实现步骤：**
1. 在 REPL 主循环中维护一个 `TokenTracker` 累计每次调用的 token 数
2. 添加 `/cost` 命令展示当前会话的 token 用量和估算费用
3. 可选：在 prompt-toolkit 的 bottom_toolbar 中实时显示累计 token 数
4. 可选：按模型配置单价（config.toml 中添加 `[cost]` 段）

**组件：**
```python
# src/memory/token_tracker.py
@dataclass
class TokenTracker:
    total_input: int = 0
    total_output: int = 0
    call_count: int = 0

    def record(self, response: LLMResponse) -> None: ...
    def estimate_cost(self, model: str) -> float: ...
    def summary(self) -> str: ...
```

---

## 第三阶段：高级代码智能

> 目标：从"通用工具"升级为"代码专家"，具备语义级代码理解能力。
> 建议耗时：每个功能 2-4 周

### 3.1 LSP 集成（语义代码分析）

**优先级：高（但复杂度也高）** — 这是让智能体真正理解代码结构的关键。

**Claude Code 中的设计：**
- 通过 LSP 协议与语言服务器通信（Python 用 pylsp/pyright，TS 用 tsserver 等）
- 提供的能力：go-to-definition、find-references、diagnostics、hover、completion
- 智能体可以调用 `getDiagnostics` 检查代码错误，而不是靠运行 bash 命令

**需要实现的组件：**

```
src/lsp/
├── __init__.py
├── client.py          # LSP 客户端：JSON-RPC over stdio（和 MCP 共享传输层）
├── manager.py         # 语言服务器生命周期管理（按语言启动对应服务器）
├── protocol.py        # LSP 消息类型定义
└── tools.py           # 暴露给智能体的 LSP 工具（getDiagnostics, goToDefinition 等）
```

**关键学习点：**
- LSP 协议（比 MCP 更复杂，有完整的文档同步、增量更新机制）
- 语言服务器发现与配置
- 与 MCP transport 层的复用

**建议的 LSP 工具：**
```
getDiagnostics   — 获取文件的语法/类型错误
goToDefinition   — 跳转到符号定义
findReferences   — 查找符号的所有引用
getHover         — 获取符号的类型信息和文档
```

**实现步骤：**
1. 复用 MCP 的 JSON-RPC transport 层
2. 实现 LSP 初始化握手（initialize → initialized → textDocument/didOpen）
3. 实现 textDocument/diagnostic 调用
4. 实现 textDocument/definition 和 textDocument/references
5. 注册为智能体工具
6. 配置：在 config.toml 中声明语言服务器

---

### 3.2 智能代码操作（semantic rename / smart relocate）

**优先级：中** — 依赖 LSP 集成。

**Claude Code 中的设计：**
- `semanticRename`：重命名符号时自动更新所有引用（通过 LSP textDocument/rename）
- `smartRelocate`：移动/重命名文件时自动更新所有 import 路径

**实现步骤：**
1. 基于 LSP 的 textDocument/rename 实现 `semantic_rename` 工具
2. 基于 LSP 的 workspace/willRenameFiles 实现 `smart_relocate` 工具
3. 回退策略：LSP 不可用时，用 grep + 正则做尽力替换

---

### 3.3 Git Worktree 隔离

**优先级：中** — 让子智能体在隔离环境中工作。

**Claude Code 中的设计：**
- 为子智能体创建临时 git worktree（独立的工作目录和分支）
- 子智能体的所有文件操作都在 worktree 中进行，不影响主工作区
- 任务完成后，如果有改动则保留 worktree 和分支；无改动则自动清理

**需要实现的组件：**

```
src/planning/
└── worktree.py        # Git worktree 创建、清理、状态检查
```

**实现步骤：**
1. 实现 worktree 创建（`git worktree add`）
2. 修改 `run_subagent` 支持 `isolation="worktree"` 参数
3. 在 worktree 中重新绑定所有文件操作 handler 的 workspace 路径
4. 任务完成后检查是否有改动，决定保留或清理
5. 返回 worktree 路径和分支名供用户后续操作

---

## 第四阶段：生产化打磨

> 目标：让项目从"学习项目"走向"可日常使用"的工具。
> 可以按需挑选实现。

### 4.1 Cron / 定时任务调度

让智能体支持定时执行任务（如定期检查 CI 状态、轮询部署进度）。

**实现要点：**
- 基于 `threading.Timer` 或 `sched` 模块的简单调度器
- `/loop` 命令：按间隔重复执行指定命令
- 任务列表管理（创建、查看、取消）

### 4.2 更完善的错误恢复与重试策略

**当前状态：** LLM 调用失败时直接抛 `LLMCallError`，用户需要手动重试。

**改进方向：**
- 可配置的自动重试（指数退避）
- 区分可重试错误（rate limit、网络超时）和不可重试错误（认证失败、模型不存在）
- 部分失败恢复（多个工具调用中某个失败，不影响其他）

### 4.3 配置热重载

**当前状态：** 配置在启动时一次性加载，修改后需要重启。

**改进方向：**
- 监听 config.toml / config.local.toml 的文件变更
- 支持 `/reload` 命令手动触发重载
- 区分可热重载的配置（theme、permission mode）和需要重启的配置（provider）

### 4.4 插件系统

将当前的 skills 系统升级为更完整的插件机制：
- 插件可以注册新工具（不仅仅是 prompt 注入）
- 插件可以注册新的 slash 命令
- 插件可以 hook 到智能体生命周期事件
- 支持从 PyPI 安装插件

---

## 建议的学习顺序总结

```
阶段一（工具层）          阶段二（质量层）          阶段三（智能层）          阶段四（生产层）
┌─────────────┐      ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ 1.1 MCP 客户端│─────→│ 2.1 Hooks   │─────→│ 3.1 LSP 集成 │─────→│ 4.1 Cron    │
│ （最高优先级） │      │             │      │ （复用 MCP    │      │ 4.2 重试策略 │
├─────────────┤      ├─────────────┤      │  transport） │      │ 4.3 热重载   │
│ 1.2 Web 工具 │      │ 2.2 持久记忆 │      ├─────────────┤      │ 4.4 插件系统 │
├─────────────┤      ├─────────────┤      │ 3.2 语义重命名│      └─────────────┘
│ 1.3 多模态读取│      │ 2.3 Token 追踪│     │ 3.3 Worktree │
└─────────────┘      └─────────────┘      └─────────────┘
```

**核心建议：从 MCP 客户端开始。** 原因：
1. 它是整个扩展体系的基石 — 实现后，Web 工具、文档查询、IDE 集成都可以通过 MCP 服务器接入
2. JSON-RPC transport 层可以直接复用到 LSP 集成
3. 学习价值最高 — 涵盖子进程管理、协议设计、动态工具注册等核心概念
4. 社区生态丰富 — 有大量现成的 MCP 服务器可以用来测试
