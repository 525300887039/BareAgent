# MCP 客户端

## Goal

为 BareAgent 增加 **Model Context Protocol (MCP) 客户端**，让用户在 `config.toml` 中声明 MCP 服务器后，BareAgent 启动时自动拉起 MCP 子进程（或连接远程 HTTP/SSE 端点），通过 JSON-RPC 发现并注入远程 tools / resources / prompts 到智能体上下文，使智能体能透明调用任意符合 MCP 标准的外部工具。这是 BareAgent 后续扩展生态（Web 工具升级、LSP 集成）的基石——之后的扩展都可以通过 MCP 服务器接入而无需在 `src/core/handlers/` 里手写。

## Requirements

### 协议覆盖
- **tools**：`tools/list` 启动时拉取并注入到 `get_tools()`；`tools/call` 调用透明转发
- **resources**：`resources/list` / `resources/read`。v1 暴露为 BareAgent 工具形式（如 `mcp__<server>__resource_read`），LLM 主动调用；schema 转换层预留 `inject_to_system` flag 为 v2 自动注入做准备
- **prompts**：`prompts/list` / `prompts/get`。暴露为 slash 命令 `/mcp:<server>:<prompt>` 供用户调起

### 传输层
- 抽象出 `Transport` ABC，含 `send_request` / `receive_response` / `close` 接口
- `StdioTransport`：subprocess + stdin/stdout pipe + 后台读取线程
- `HttpSseTransport`：httpx Client + Server-Sent Events 解析（请求走 POST，订阅走 SSE）
- 配置 `transport = "stdio"` 或 `"http"` 切换；HTTP 模式额外读取 `url`、`headers`

### 命名空间
- 所有 MCP 工具按 `mcp__<server>__<tool>` 命名注入 BareAgent 工具列表
- Schema 注入时建立 `name → (server_name, original_tool_name)` 映射，handler 调用时反查
- Resource read 工具名：`mcp__<server>__resource_read`；prompt 触发：REPL 命令 `/mcp:<server>:<prompt>`

### 生命周期
- **启动**：并发拉起所有配置的 server（`concurrent.futures.ThreadPoolExecutor`）；总耗时 ≈ 最慢的一个
- **启动超时**：单个 server 超过 `start_timeout`（默认 10s）未握手完成 → 标记 unhealthy + 跳过 + console 告警
- **启动失败**：warning + 跳过，BareAgent 照常运行
- **运行中崩溃**：从工具集移除该 server 的工具、推送 console 通知、标记 `unhealthy`
- **退出清理**：注册 `atexit` + 信号处理，确保所有子进程被 reap，不留僵尸

### REPL 命令
- `/mcp status`：列出所有 server 状态（running / unhealthy / stopped）+ 工具数
- `/mcp reload <name>`：重启指定 server（kill 旧进程 + 重新启动 + 重新握手）
- `/mcp list`：列出当前可用 MCP 工具完整名单（带 `mcp__` 前缀）

### 权限
- MCP 工具走现有 `PermissionGuard`：DEFAULT 模式每次询问（args 预览）、AUTO 自动通过、PLAN 拒绝、BYPASS 放行
- 危险模式检测（rm -rf 等）不应用于 MCP 工具——MCP 参数是 JSON 不是 shell 文本
- **子代理隔离**：`agent_types.py` 中 `explore` / `plan` / `code-review` 三种只读类型默认禁用所有 `mcp__` 工具（通过新增 `mcp_tools_enabled: bool = True` 字段，三个只读类型设 False）

### 结果回传 / 多模态
- 修改 `src/core/loop.py::_tool_result` 接受多内容块（text + image + embedded resource）
- Provider 适配：
  - Anthropic：`{type: "image", source: {type: "base64", media_type, data}}`
  - OpenAI：`{type: "image_url", image_url: {url: "data:<mime>;base64,<data>"}}`
- `isError: true` 加 `Error: ` 前缀；JSON-RPC error → `MCP Error: <code> <message>`

### Payload 上限
- 单次 tool result 文本超过 `max_result_text_bytes`（默认 256 KB）→ 截断 + `[truncated, original size: N bytes]`
- 单个 image / embedded resource 超过 `max_result_binary_bytes`（默认 5 MB）→ 替换为 `[Resource omitted: too large (N bytes)]` 占位文本
- 都通过 config.toml `[mcp]` 段全局配置

### 配置示例
```toml
[mcp]
start_timeout = 10
max_result_text_bytes = 262144
max_result_binary_bytes = 5242880

[[mcp.servers]]
name = "context7"
transport = "stdio"
command = "npx"
args = ["-y", "@context7/mcp-server"]
env = { CONTEXT7_API_KEY = "..." }

[[mcp.servers]]
name = "team-knowledge"
transport = "http"
url = "https://mcp.example.com/sse"
headers = { Authorization = "Bearer ${KNOWLEDGE_TOKEN}" }
```

## Acceptance Criteria

- [ ] `[[mcp.servers]]` 配置加载、stdio 模式启动并发握手成功，工具按 `mcp__<server>__<tool>` 注入到 `get_tools()` 返回值
- [ ] HTTP/SSE transport 至少能连一个已知公共 MCP 服务并完成 tools/list
- [ ] LLM 调用 `mcp__fetch__fetch` 工具能透明转发到 fetch server 并返回结果
- [ ] `resources/list` 暴露为工具，LLM 调用后能读到 server 资源
- [ ] `prompts/list` 在 REPL 启动后可通过 `/mcp:<server>:<prompt>` 触发
- [ ] DEFAULT 模式调用 MCP 工具触发 ask_user 提示（含参数预览）
- [ ] explore / plan / code-review 子代理调用 `mcp__*` 工具被拒绝
- [ ] kill MCP 子进程 → REPL 报警 + 工具从 LLM 视图消失 + `/mcp status` 显示 unhealthy
- [ ] `/mcp reload <name>` 能恢复挂掉的 server
- [ ] Server 启动超过 10s 不响应 → 跳过 + 报警 + 不阻塞 REPL boot
- [ ] BareAgent 退出后无 MCP 僵尸进程（手动 `ps` / `tasklist` 验证）
- [ ] 256KB 以上的 text result 自动截断 + 提示
- [ ] MCP server 返回 image content → LLM 能在下一轮看到（端到端冒烟，需 Anthropic + OpenAI 都验证）
- [ ] 12 个以上 pytest case 覆盖关键路径（详见 Definition of Done）

## Definition of Done

- 新增 `src/mcp/` 子包，结构遵循 `.trellis/spec/backend/directory-structure.md`
- pytest 覆盖三层：transport（含 mock subprocess / mock SSE）、protocol（JSON-RPC encode/decode + 握手 FSM）、registry（命名转换 + 注入 + 反查）
- `ruff check src tests` / `ruff format src tests` 全绿
- `tests/test_mcp_*.py` 必含：握手成功 / 握手超时 / 工具调用成功 / 工具调用 JSON-RPC error / 子进程崩溃恢复 / `/mcp reload` / payload 截断 / image content 双 provider 适配 / 子代理拒绝 mcp__ / `mcp__` 名称冲突时 fail-fast
- `CLAUDE.md` 与 `.trellis/spec/backend/directory-structure.md` 增加 `src/mcp/` 模块说明
- `config.toml` 增加完整 `[[mcp.servers]]` 示例（stdio + http 各一）+ 注释
- 端到端冒烟：用 `mcp-server-fetch`（uvx）跑通 stdio 全流程；如果方便，准备一个最小 HTTP/SSE echo 服务验 http transport（也可推到后续）

## Technical Approach

### 模块布局
```
src/mcp/
├── __init__.py           # 公共导出
├── client.py             # 单个 MCP server 的客户端：握手、调度、生命周期
├── manager.py            # 多 server 管理（并发启动、状态追踪、重启）
├── protocol.py           # JSON-RPC 2.0 + MCP 消息类型（dataclasses）
├── registry.py           # tool/resource/prompt schema 注入 get_tools/get_handlers
├── transport/
│   ├── __init__.py
│   ├── base.py           # Transport ABC
│   ├── stdio.py          # subprocess + pipe 实现
│   └── http_sse.py       # httpx + SSE 实现
├── config.py             # [[mcp.servers]] 配置解析
└── errors.py             # MCPError / MCPHandshakeError / MCPCallError 等
```

### 关键集成点
1. **`src/main.py::load_config`**：新增 `[[mcp.servers]]` 段解析 → 产出 `MCPConfig`
2. **`src/main.py::main`**：在 `agent_loop()` 前调用 `MCPManager.start_all()`；注册 `atexit` 清理
3. **`src/core/tools.py::get_tools` / `get_handlers`**：接受 `mcp_manager` 参数，注入 MCP schemas 和 handlers
4. **`src/core/loop.py::_tool_result`**：扩展为支持多内容块（list of `{type, ...}`）
5. **`src/provider/anthropic.py` + `openai.py`**：消息序列化层适配 image block 转换
6. **`src/planning/agent_types.py`**：`AgentType` 增加 `mcp_tools_enabled: bool = True`；`explore` / `plan` / `code-review` 设 `False`；`filter_tools` 应用过滤
7. **`src/main.py` REPL 命令**：新增 `/mcp status` / `/mcp reload` / `/mcp list` 路由

### 关键依赖
- `httpx`（已传递依赖于 anthropic SDK）
- 自写 SSE 解析（< 50 LOC，避免引 `httpx-sse` 这种小依赖）
- 标准库：`subprocess`、`threading`、`concurrent.futures`、`json`、`atexit`、`signal`

## Decision (ADR-lite)

**Context**：BareAgent 当前所有工具都是本地 Python 函数，无法接入 Anthropic 主导的 MCP 生态（fetch / postgres / git / filesystem 等几十个开源 server）。这限制了智能体扩展能力。

**Decision**：实现完整 MCP 客户端（tools + resources + prompts，stdio + HTTP/SSE 双传输），与现有权限模型、子代理类型系统、loop 消息层深度集成。结果回传走结构化 passthrough 而非字符串化，顺带把 ROADMAP 1.3 多模态读取的核心改动做掉。

**Consequences**：
- ✅ 所有未来外部工具集成（Web 升级、IDE 集成、第三方服务）走 MCP 即可，不再加内置 handler
- ✅ JSON-RPC transport 层为 LSP 集成（ROADMAP 3.1）打基础，可复用
- ✅ 多模态 image 通路打通，ROADMAP 1.3 的 PDF/Notebook 读取改造也更顺
- ⚠️ Scope 较大（估 1500-2500 LOC + 测试），建议拆分为 4-6 个 PR 渐进落地
- ⚠️ 引入新失败面：子进程管理、网络连接、JSON-RPC id 路由——测试必须覆盖到位

## Out of Scope (explicit)

- **Sampling**：MCP server 反向请求 LLM 推理（需为 server 暴露 provider 抽象，架构侵入太大）
- **BareAgent 作为 MCP server**：v1 只做 client
- **Per-server / per-tool 权限策略**：v1 走全局 PermissionGuard；v2 可在 `[[mcp.servers]]` 加 `auto_approve: list[str]`
- **Resource 自动注入 system prompt**：v1 仅预留 schema flag，不实现注入逻辑
- **MCP server 配置热重载**：编辑 `config.toml` 不重启的能力，留给 ROADMAP 4.3 配置热重载任务
- **Skill 与 MCP prompts 统一**：两者保持独立，未来考虑

## Technical Notes

- 参考 [MCP 官方规范](https://modelcontextprotocol.io)（需 trellis-research 拉取最新版本细节）
- 参考 [`mcp-server-fetch`](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) 作为 stdio 端到端测试目标
- Claude Code 命名约定 `mcp__<server>__<tool>` 用两个下划线分隔，避免与单下划线工具名冲突
- 子进程管理可借鉴 `src/concurrency/background.py` 的 daemon 线程模式
- `src/core/loop.py` 现 `_tool_result` 改动需小心：tool result 是 LLM 消息流的核心数据，回归测试要全
- BareAgent 现有 provider 模型的消息序列化在各 provider 实现里——image block 转换在 provider 层做，不污染 loop

## Research References

_v1 实施前应通过 trellis-research（或在本会话无 sub-agent 时改为 general-purpose / inline）拉取以下主题，写入 research/：_

- `research/mcp-protocol-spec.md` — MCP 最新规范要点（initialize / tools / resources / prompts 各方法 schema）
- `research/json-rpc-edge-cases.md` — JSON-RPC 2.0 错误码、批处理、id 路由的最佳实践
- `research/popular-mcp-servers.md` — 主流 MCP server 的工具/资源/提示词模式抽样，验证 schema 转换的假设
- `research/sse-parsing-minimal.md` — SSE 协议的最小可用解析实现
