# PR3: MCP Resources + Prompts 支持

> 父任务：`.trellis/tasks/05-27-mcp`（完整 MCP 客户端规划，决策与研究见父 PRD + `research/`）
> 前置 PR：PR1（transport + protocol）✅、PR2（client + manager + registry + tools 注入）✅

## Goal

在 PR2 的 `MCPClient` / `MCPManager` / `registry.py` 基础上扩展 **MCP resources 与 prompts** 支持。

- **Resources**：每个 server 注入两个工具 `mcp__<server>__resource_list` + `mcp__<server>__resource_read` 到 BareAgent 工具列表，LLM 主动调用——v1 不做自动注入 system prompt（父 PRD 已明确留 v2）
- **Prompts**：REPL 启动后通过 `/mcp:<server>:<prompt> [arg=value ...]` 触发，把 `prompts/get` 返回的 messages 注入到对话上下文

本 PR **不动** loop._tool_result 的多内容块结构（PR5）、不动 PermissionGuard / agent_types 隔离（PR4）、不做 atexit / payload 截断 / `/mcp reload`（PR6）。

## Requirements

### Resources 暴露
- 每个 RUNNING 且声明 `resources` capability 的 server 注入两个 BareAgent 工具：
  - `mcp__<server>__resource_list` —— 无参数（v1 不做分页），返回 `resources/list` 的拍平结果（URI / name / description / mimeType 串联文本）。LLM 用它发现资源
  - `mcp__<server>__resource_read` —— 必填参数 `uri: string`，调 `resources/read`，把返回的 `contents` 中所有 `type=text` 拍平串联返回。非 text content（blob / image 等）降级为 `[<type> omitted: PR5]` 占位文本
- Tool schemas 走 PR2 已有的 `registry.py` 注入路径（追加到 `build_mcp_tool_schemas` / `build_mcp_handlers` 即可，不新开模块）
- 名称冲突 fail-fast 继承 PR2 现有行为

### Prompts 暴露
- 启动握手成功后，对声明 `prompts` capability 的 server 调 `prompts/list`，缓存到 `MCPClient._prompts: list[dict]`，与 `list_tools()` 同期完成（不重复请求）
- 同样在 `client.start()` 内串行：`initialize` → `notifications/initialized` → 按 capability 各自调 `tools/list` / `resources/list` 占位（v1 不缓存 resources，因为 read 是动态的）/ `prompts/list`
- **能力门控**：`MCPClient.start` 解析 server 声明的 `capabilities` 字段，记录到 `client.server_capabilities: dict[str, dict]`；后续 list_* 调用前先检查；缺失 capability 直接跳过该 list（不抛、不报错）
- REPL 命令：`/mcp:<server>:<prompt> [key=value key2=value2 ...]`
  - 解析：冒号分两段拿到 `server_name` / `prompt_name`；剩余 token 按 `key=value` 解析为 dict（v1 不支持 quoted value with spaces，遇到非 `key=value` 形式 warn 并跳过该 token）
  - 调 `client.get_prompt(prompt_name, arguments)`，返回 `prompts/get` 的 `messages` 数组
  - 把 messages 中每条 `{role, content}` 转成 BareAgent transcript 的标准消息形态 append 进当前 session transcript：
    - `role: "user"` → 普通 user 消息
    - `role: "assistant"` → assistant 消息
    - content 拍平：text 串联；非 text 块降级为 `[<type> omitted: PR5]`
  - append 完成后触发下一轮 `agent_loop()`（与正常 user 输入流程一致）

### 命令命名空间
- `/mcp:` 前缀（带冒号）专门给 prompts 路由——与 PR4 即将加的 `/mcp ` 状态命令（带空格）显式区分
- 不合法的 prompt name（含 `[a-zA-Z0-9_-]` 之外字符的）在 `prompts/list` 返回时 warn 并跳过，不注册到路由表

### 错误处理
- 工具 handler 的错误降级路径完全复用 PR2 `registry.py` 已有模式（`MCPCallError` → 字符串、`isError:true` → `Error: ` 前缀、unhealthy server → 字符串）
- `/mcp:<server>:<prompt>` 命中 unhealthy / 不存在的 server / prompt → REPL 打印 `Error: ...` + 不消耗 token
- `prompts/get` JSON-RPC error → REPL 打印 `MCP Error: <code> <message>` + 不修改 transcript

### Capability 声明（client-side）
- PR2 `initialize` 发出的 client capabilities 是空 dict；本 PR 维持空（不声明 sampling/elicitation/roots，那是 server-callable，本 v1 不支持）

## Acceptance Criteria

- [ ] `mcp-server-fetch` 启动后 `mcp__fetch__resource_list` / `mcp__fetch__resource_read` 工具出现在 `get_tools()` 中
- [ ] `mcp-server-everything` 这类有 prompts 的 server 启动后，REPL `/mcp:everything:<prompt_name>` 能触发并把 messages 注入 transcript + 触发 agent_loop 下一轮
- [ ] server 不声明 `resources` capability → 不注入两个 resource 工具（不 leak）
- [ ] server 不声明 `prompts` capability → 不在 REPL 路由表里出现 `/mcp:<server>:*`
- [ ] `resources/read` 返回 blob 内容 → tool handler 返回占位 `[blob omitted: PR5]` 而不是抛异常
- [ ] `prompts/get` 返回 JSON-RPC error → REPL 打印 `MCP Error:` 字符串，transcript 不被修改
- [ ] PR2 现有测试 / acceptance 不退化
- [ ] 至少 8 个 pytest case 覆盖关键路径

## Definition of Done

- 改动尽量集中在 `src/mcp/client.py` + `src/mcp/registry.py` + `src/main.py` REPL 路由
- 新增独立测试文件 `tests/test_mcp_prompts.py`（registry resources 覆盖追加到 `test_mcp_registry.py`）
- `ruff check src tests` / `ruff format src tests` 全绿
- pytest 全集合 green，不退化 PR1 / PR2 测试
- 不动 `src/core/loop.py` / `src/provider/*` / `src/permission/*` / `src/planning/agent_types.py`

## Technical Approach

### `src/mcp/client.py` 改动
- `MCPClient` 增加 `_prompts: list[dict] | None`、`server_capabilities: dict[str, Any]`
- `start()` 流程：
  1. `transport.start()`
  2. 发 `initialize` → 解析 `result.capabilities` 存到 `server_capabilities`
  3. 发 `notifications/initialized`
  4. 按 capability 拉初始数据：
     - `tools` 在 → 先不调，保持 lazy（与 PR2 行为一致，`list_tools()` 仍在 manager.start_all 完成后由 registry 第一次调时拉）
     - `prompts` 在 → 调 `prompts/list` 缓存到 `_prompts`
     - `resources` 在 → 不预拉（resources 是动态 + 大），保持 list_resources() lazy
- 新增方法：
  - `list_prompts() -> list[dict]`：返回缓存
  - `get_prompt(name, arguments) -> dict`：调 `prompts/get`，复用与 `call_tool` 同款双层错误处理
  - `list_resources() -> list[dict]`：调 `resources/list`，不缓存（动态资源）
  - `read_resource(uri) -> dict`：调 `resources/read`，双层错误处理
  - `has_capability(name: str) -> bool`：查 `server_capabilities`

### `src/mcp/registry.py` 改动
- `build_mcp_tool_schemas` / `build_mcp_handlers` 对每个 client 追加：
  - 仅当 `has_capability("resources")` 时注入 `mcp__<server>__resource_list` + `mcp__<server>__resource_read`
- Tool schema 写死（不是 server 提供的）：
  - `resource_list`：`{type: "object", properties: {}, required: []}`
  - `resource_read`：`{type: "object", properties: {uri: {type: "string"}}, required: ["uri"]}`
- Handler 内部调 `client.list_resources()` / `client.read_resource(uri)`，错误降级走现有 `_handle_call_result` 模式

### `src/main.py` 改动
- REPL 命令路由（slash command 段，~line 1291-1452）追加 `/mcp:` 分支：
  - `text.startswith("/mcp:")` → 解析 `server` / `prompt` / kwargs，调 `_dispatch_mcp_prompt(...)` 辅助函数
  - 辅助函数在同文件就近定义即可（不新开模块——PR4 加 `/mcp status` 时再考虑提取到 `src/mcp/repl.py`）
- 不动现有 `_run_stdio_session` 主循环（消息 append + agent_loop 下一轮触发与 `/resume` 等命令同款 pattern）

### 错误降级 / 占位策略
- 复用 PR2 `_handle_call_result()` 同款拍平逻辑——可重构成 registry 内部 `_flatten_content(content: list[dict]) -> str` 公共函数，resources/read + prompts content + tools/call result 共用

## Decision (ADR-lite)

**Context**：父 PRD v1 已锁定 resources = 工具 / prompts = slash 命令两个方向，PR3 范围是把这两个落地到 PR2 已有的 manager + registry + REPL 框架里。

**Decision**：
- 每 server 注入 `resource_list` + `resource_read` 两个工具；不暴露每个 resource 单独为工具（resources 是动态的、可能大量）
- prompts 启动时缓存到 client，REPL 路由表用 `/mcp:` 前缀
- 复用 PR2 已有错误降级 + content 拍平模式，不重新发明

**Consequences**：
- ✅ LLM 透明地把 MCP resources 当工具用，不需要 system prompt 注入
- ✅ Prompts 走 slash 命令符合"用户触发"的 MCP 语义
- ✅ PR4 / PR5 不需要改动 registry 的 resource handler 结构（PR5 改 `_flatten_content` 内部即可全 transports 通过）
- ⚠️ 每 server 至少注入 2 个工具（resource_list + resource_read），会推高 tool schema 数量；如果一个 BareAgent 配 5 个 server 全都有 resources → 多 10 个工具——可接受

## Out of Scope (explicit)

- **Resources 自动注入 system prompt**：v1 维持 LLM 主动调（父 PRD 已声明）
- **Resources 分页**：`resources/list` 的 `cursor` / `nextCursor` —— v1 只取首页，warn 如果有 nextCursor
- **Resource templates** (`resources/templates/list`)：主流 server 罕用，暂缓
- **Resource subscriptions** (`resources/subscribe` + `notifications/resources/list_changed`)：留 PR6 hardening
- **Prompts 参数 quoted value with spaces**：v1 `/mcp:server:prompt key=value` 只支持无空格 value；含空格的暂走不上去（warn + 跳过）
- **Prompts 参数验证**：不校验 server 声明的 `arguments` schema，盲传，让 server 自己回 error
- **Resource MIME type 智能 dispatch**：v1 不按 mimeType 自动 base64 / 解码，PR5 multimodal 再做
- **多内容块 / image / audio / embedded_resource 原生回传**：PR5
- **PermissionGuard 集成 / 子代理隔离**：PR4
- **REPL `/mcp status` / `/mcp list` / `/mcp reload`**：PR4 / PR6
- **atexit / signal cleanup**：PR6

## Technical Notes

- 父任务 PRD 见 `../05-27-mcp/prd.md`
- 关键研究：
  - `../05-27-mcp/research/mcp-protocol-spec.md` — resources/list、resources/read、prompts/list、prompts/get 的完整 schema + capabilities 协商
  - `../05-27-mcp/research/popular-mcp-servers.md` — server 端 resources / prompts 暴露习惯
- 复用 PR2 现有模式：
  - `src/mcp/client.py::MCPClient.call_tool` 的双层错误（JSON-RPC error → 抛；`isError:true` → 不抛）→ resources/read + prompts/get 同款复用
  - `src/mcp/registry.py` 现有 `_flatten_content` 模式 → 公共化给 resource_read + prompt content 共用
  - `src/main.py` `/resume` / `/clear` 等命令分支模式 → `/mcp:` 路由照搬
- 必须遵循 `.trellis/spec/backend/`：
  - `error-handling.md`：MCP error 一律走 `MCPCallError` / 字符串降级，不裸抛
  - `logging-guidelines.md`：REPL 反馈走 `UIProtocol`，warn 走 logger
  - `directory-structure.md`：新增工具不在 `src/core/handlers/`，仍走 registry 注入
  - `quality-guidelines.md`：from __future__ import annotations + 类型注解 + ruff
