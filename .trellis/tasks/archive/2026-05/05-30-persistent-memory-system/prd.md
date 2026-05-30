# 持久化记忆系统（Persistent Memory System）

> ROADMAP 2.2 · 文件式 agent 记忆 · 工具契约对齐 Anthropic memory tool（但注册为普通 client tool，全 provider 可用）

## Goal

让 BareAgent 跨会话记住用户偏好（user）、行为反馈（feedback）、项目上下文（project）、外部引用（reference）。
采用「文件式 agent 记忆」：一条记忆 = 一个带 frontmatter 的 `.md` 文件，`MEMORY.md` 作索引；
智能体通过一个对齐 Anthropic memory tool 契约的专用工具读写受限的 `memory/` 目录；
会话开局把索引 + MEMORY PROTOCOL 注入 system prompt，对话中按需增删改。
不绑定 Anthropic 原生 `memory_20250818` 类型 —— 注册为普通 client tool，保证 OpenAI/DeepSeek 等 provider 都能用。

## What I already know

### 用户给定的设计基线（硬约束）
- **工具契约对齐 Anthropic memory tool**：实现 `view` / `create` / `str_replace` / `insert` / `delete` / `rename` 六个命令，作用于受限的 `memory/` 目录。
- **注册为普通 client tool**：schema 进 `core/schema.py` 体系、handler 进 `core/handlers/`；**不**用 Anthropic 原生 `memory_20250818` 工具类型，让逻辑层 provider-agnostic。
- **MEMORY PROTOCOL 自注入**：「动手前先 view 记忆目录」由 BareAgent 注入 system prompt，不依赖 provider 端原生 memory 语义。
- **上层能力**：`MEMORY.md` 索引（会话开局注入前 N 行）+ frontmatter 分类（user/feedback/project/reference）+ `/remember`、`/forget` 命令 + `assemble_system_prompt` 注入。
- **安全**：路径穿越防护，复用 `src/core/sandbox.py` 的 `safe_path`（resolve + is_relative_to + 拒绝符号链接/绝对路径/家目录相对路径）。
- **隔离**：沿用 AgentType 开关，子代理（explore/plan/code-review）默认**只读**记忆。
- **不引入** mem0/向量库（避免过度设计），但接口要为未来「在同一 memory 工具背后挂向量检索后端」留升级位。

### Anthropic memory tool 契约（对齐目标，命令 + 参数）
| command | 参数 | 语义 |
|---|---|---|
| `view` | `path`, `view_range?: [start,end]` | 列目录 / 看文件（可带行区间，1-based） |
| `create` | `path`, `file_text` | 新建/覆盖文件 |
| `str_replace` | `path`, `old_str`, `new_str` | 唯一匹配替换 |
| `insert` | `path`, `insert_line`, `insert_text` | 指定行后插入 |
| `delete` | `path` | 删除文件/目录 |
| `rename` | `old_path`, `new_path` | 重命名/移动 |

Anthropic 原生用 `/memories/...` 绝对前缀；我们的普通 client tool 版改为**记忆根目录相对路径**，统一走 `safe_path(workspace=memory_root)` 校验（或保留 `/memories` 前缀并内部 strip —— 见 Open Questions）。

### 现有代码集成点（已由 Explore 子代理勘探，文件:行号可直接照搬）
- **Schema helper**：`src/core/schema.py:6` `tool_schema(name, description, properties, required)`（别名 `_schema`）。
- **工具注册总线**：`src/core/tools.py` —— `BASE_TOOLS`(34)、`DEFERRED_TOOLS`(44)、`TOOL_SCHEMAS`(131)、`DEFERRED_TOOL_SCHEMAS`(121)、`get_tools()`(422)、`get_handlers()`(438，依赖通过 `functools.partial` 闭包注入 workspace 等)。
- **handler 模式**：`src/core/handlers/*.py`，签名如 `run_read(file_path, ..., *, workspace: Path) -> str`；在 `get_handlers()` 内 `partial(fn, workspace=...)` 绑定。
- **system prompt 组装**：`src/core/context.py:83` `assemble_system_prompt(workspace, skill_summary, nag_reminder)`；段落用 `<tag>...</tag>` 包裹拼接。调用点 `main.py` `_initial_messages()`。仿 `get_user_context()` 加 `get_memory_context()`。
- **REPL slash 命令**：`src/main.py` —— 命令名列表 `_SLASH_COMMANDS`(471)，主循环 if/elif 分发(1650+)，已有 `_handle_log_command`/`_handle_team_command` 等抽提函数可仿。
- **配置解析**：`src/main.py:232` `load_config()`，各 section（`[mcp]`/`[lsp]`）解析后塞进 `Config` dataclass(118)；仿 `parse_mcp_config()` 加 `[memory]`。
- **AgentType 过滤**：`src/planning/agent_types.py:102` `filter_tools()` —— **只按工具名 + 前缀（mcp__/lsp_）过滤，无子命令粒度**；`_READ_ONLY_DEFAULTS`(36) 用 `disallowed_tools` 列表 + `*_tools_enabled` 布尔开关。subagent 调用点 `src/planning/subagent.py:141`。
- **现有 `src/memory/`**：`compact.py`（上下文压缩）、`transcript.py`（会话持久化）、`token_counter.py`（token 估算）。ROADMAP 提议的 `persistent.py`/`index.py`/`types.py` **均不存在**，需新建。

### 关键架构张力（决定工具形态）
`filter_tools` 只能整工具名级别 allow/deny。若按 Anthropic 契约做**单个 `memory` 工具 + `command` 枚举**，read-only 子代理无法「只放行 view」——必须靠 handler 层读 `read_only` 标志拦截写命令，或新增 AgentType 维度（如 `memory_writable: bool`）。若拆成**多个独立工具**（`memory_view`/`memory_create`/...），则现有 `disallowed_tools` 机制天然可只读，但偏离「对齐 Anthropic 单工具契约」的形态。详见 Open Questions Q1。

## Decision (ADR-lite) — 2026-05-30 已拍板

**Context**：工具形态、只读隔离机制、存储位置、命令语义、路径前缀需在动工前定死。

**Decision**：
- **D1（工具形态 × 只读，方案 A）**：单个 `memory` 工具 + `command` 枚举（view/create/str_replace/insert/delete/rename）。只读隔离 = `AgentType` 新增 `memory_writable: bool`（仿 `mcp_tools_enabled`/`lsp_tools_enabled`），`_READ_ONLY_DEFAULTS` 设 `memory_writable=False`；handler 闭包注入该标志，写命令在 `False` 时返回错误字符串；工具 description 注明子代理只读。
- **D2（存储位置）**：可配置 `[memory] dir`，默认全局 `~/.bareagent/projects/<workspace-slug>/memory/`（项目隔离 + 集中存储，不污染项目 git）。`<workspace-slug>` 由 workspace 绝对路径派生（仿 Claude Code 的 `D--code-BareAgent` 风格）。
- **D3（/remember、/forget 语义）**：注入一条用户指令，让 LLM 经 memory 工具落盘/删除（顺带 distill + 归类 + 维护 `MEMORY.md` 索引），不做裸文件操作 —— 保证索引与记忆文件不脱节。
- **D4（path 前缀）**：工具接受**记忆根相对路径**，统一 `safe_path(path, workspace=memory_root)` 校验；若模型传入 `/memories/...` 或 `memory/...` 前缀，内部 strip 兼容。

**Consequences**：
- 只读为 handler 层拦截（非 schema 隐身）——写命令对子代理可见但调用即拒；安全等价，可接受。
- `memory_writable` 是新增 AgentType 维度，需同步 `filter_*`/subagent 注入链与测试。
- 全局存储需 workspace→slug 派生函数 + 目录懒创建；首次运行自动 `mkdir`。
- 单工具 command 路由集中在一个 handler，未来挂向量后端只改这一处（满足升级位要求）。

## Requirements（evolving）

- R1. 新增**单个** `memory` 工具（`command` 枚举 view/create/str_replace/insert/delete/rename），记忆根相对路径经 `safe_path` 限制在 memory 根内（D4：兼容 strip `/memories/`、`memory/` 前缀）。
- R2. 一条记忆 = 一个带 YAML frontmatter（name/description/type）的 `.md` 文件；`MEMORY.md` 为单行索引（每条一行 `- [title](file.md) — hook`）。
- R3. frontmatter 分类枚举：user / feedback / project / reference。
- R4. 会话开局把 `MEMORY.md` 索引（前 N 行，N 可配，默认全文若小于上限）+ MEMORY PROTOCOL 段落注入 system prompt（`get_memory_context()`）。
- R5. `/remember`、`/forget` slash 命令（D3：注入用户指令驱动 LLM 经工具落盘/删除）。
- R6. `[memory]` 配置 section：`enabled`、`dir`（默认全局按项目 slug）、`max_index_lines`（注入上限）等。
- R7. 子代理只读隔离：`AgentType.memory_writable`（D1），explore/plan/code-review 默认 `False`，写命令 handler 层被拒。
- R8. MemoryManager 检索/搜索方法定义为可替换接口（MVP = frontmatter + 子串匹配），预留向量后端升级位。
- R9. 路径安全：复用 `src/core/sandbox.py:safe_path`，拒绝穿越/绝对/家目录/符号链接。

## Acceptance Criteria（已全部满足）

- [x] memory 工具六命令各有 pytest 覆盖（含路径穿越被拒、唯一匹配失败、view 行区间）。`tests/test_persistent.py` + `tests/test_memory_tool.py`。
- [x] 子代理只读：read-only AgentType 下写命令被拒（`test_readonly_wrapper_allows_view_rejects_writes` + `test_readonly_agent_types_cannot_write_memory`）。
- [x] 会话开局 system prompt 含 memory 索引 + protocol（`test_assemble_system_prompt_includes_memory_context` + `system_prompt_section` 测试）。
- [x] `/remember`、`/forget` 端到端可用（指令构建器有测试；REPL 分发改写 text 复用 agent_loop 块）。
- [x] `[memory]` 配置解析 + 默认值测试（`test_load_config_parses_memory_section` 等）。
- [x] ruff check 干净；新增 11 文件 ruff format 干净；全量 567 passed / 0 failed（512 基线 +55 新）。

## Definition of Done

- 单元/集成测试覆盖六命令 + 隔离 + 注入 + 配置；ruff 干净；
- CLAUDE.md「架构」节补一段 memory 子系统说明；config.toml 补 `[memory]` 默认块；
- 路径安全与只读隔离有显式测试；不破坏现有 512 passed 基线。

## Out of Scope（explicit）

- 向量检索 / mem0 / embedding（仅留接口位）。
- 跨机器同步、加密存储。
- 记忆自动过期/GC（可后续）。
- 多用户记忆隔离。
- 绑定 Anthropic 原生 `memory_20250818` 工具类型。

## Research Notes

### Q1 可行方案（工具形态 × 只读隔离）

**方案 A：单 `memory` 工具（command 枚举）+ AgentType 新增 `memory_writable` 布尔位**（推荐）
- 工具形态：1 个工具 `memory`，参数 `command: enum[view,create,str_replace,insert,delete,rename]` + 各命令所需字段。最忠实 Anthropic 契约。
- 只读隔离：仿 `mcp_tools_enabled`/`lsp_tools_enabled`，给 `AgentType` 加 `memory_writable: bool`（read-only 默认 False）；handler 闭包注入该标志，写命令在 `memory_writable=False` 时返回错误。`_READ_ONLY_DEFAULTS` 加 `memory_writable: False`。
- Pros：契约最忠实；单工具 schema 简洁；与现有 `*_enabled` 开关一致；未来挂向量后端只改一个 handler。
- Cons：只读不是「schema 层不可见」而是「handler 层拒绝」——写命令仍在子代理 schema 里出现（但调用即被拒，且可在工具 description 注明只读）。

**方案 B：多个独立工具（`memory_view`/`memory_create`/.../`memory_rename`）**
- 只读隔离：`_READ_ONLY_DEFAULTS.disallowed_tools` 直接列出五个写工具，天然 schema 层不可见。
- Pros：完全复用现有 `disallowed_tools` 机制，零新增 AgentType 维度；只读子代理连写工具 schema 都看不到。
- Cons：偏离「对齐 Anthropic 单工具契约」形态；6 个工具挤占工具列表；command 路由逻辑分散。

**方案 C：单工具 + handler 内根据 permission guard 判定**
- 复用 permission 模式（PLAN 模式拒写）而非新增 AgentType 维度。
- Cons：PLAN 是会话级权限，与「子代理只读但主代理可写」的 AgentType 级隔离语义不完全对齐；耦合权限系统，较绕。不推荐。

→ 待 Q1 拍板。

### Q2 存储位置
- 项目本地 `./.bareagent/memory/`：随项目走、可入 git；但 user/feedback 跨项目记忆无法共享。
- 全局 `~/.bareagent/projects/<workspace-slug>/memory/`：仿 Claude Code，项目隔离 + 集中存储；默认不入项目 git。
- 推荐：可配置 `[memory] dir`，默认全局按项目分目录；后续可加 global 与 project 双层（暂 Out of Scope）。

## Research References

- 现有约定见 `.trellis/spec/backend/state-persistence.md`、`directory-structure.md`、`error-handling.md`。

## Technical Notes

- 路径安全统一走 `src/core/sandbox.py:safe_path(path, workspace=memory_root)`。
- MemoryManager 放 `src/memory/`（persistent.py / index.py / types.py 或合并），与 compact/transcript 平级。
- 注入函数 `get_memory_context(memory_root) -> str` 仿 `src/core/context.py:get_user_context()`。
- 工具走 DEFERRED（按需加载），加入 `DEFERRED_TOOLS` + `DEFERRED_TOOL_SCHEMAS` + `get_handlers()` 注册。
