# PR1: MCP Transport + Protocol 脚手架

> 父任务：`.trellis/tasks/05-27-mcp`（完整 MCP 客户端规划，决策与研究见父 PRD + `research/`）

## Goal

为后续 5 个 PR 打地基：实现 `src/mcp/` 子包的最底层——`Transport` ABC + 三个具体实现（stdio / http_legacy / http_streamable）+ JSON-RPC 2.0 协议层（messages + 并发 id 路由）+ MCP server 配置解析。本 PR **不实现** Client 类、不实现握手、不实现 tool/resource/prompt schema 注入——只交付"能向 server 发 raw JSON-RPC request 并拿回 raw response 的能力"。

## Requirements

### `src/mcp/errors.py`（~30 LOC）
- `MCPError` 基类
- `MCPTransportError`（传输层故障：连接断、framing 错）
- `MCPProtocolError`（JSON-RPC 协议错：id 不存在、超时等）
- 不包含 `MCPHandshakeError` / `MCPCallError`（那些在 PR2）

### `src/mcp/protocol.py`（~80 LOC）
- 类型化 JSON-RPC 2.0 消息（dataclass）：`Request` / `Response` / `Notification` / `ErrorObject`
- 编解码函数：`encode_message(msg) -> str`（不带 trailing newline；调用方按需 append）和 `decode_message(line: str) -> Request | Response | Notification`
- id 生成器：`new_request_id() -> int`（threadsafe 自增）
- **不包含 batch 支持**（MCP 2025-06-18 已移除，见研究）
- 标准错误码常量（-32700/-32600/-32601/-32602/-32603/服务器错 -32000~-32099）

### `src/mcp/transport/base.py`（~50 LOC）
- `Transport` ABC：
  - `def start(self) -> None`：启动底层连接 / 子进程
  - `def send(self, message: str) -> None`：发送一条已编码的消息
  - `def request(self, request: Request, *, timeout: float) -> Response`：发请求并阻塞等响应（内部按 id 路由）
  - `def notify(self, notification: Notification) -> None`：发 notification（不等响应）
  - `def on_notification(self, callback: Callable[[Notification], None]) -> None`：注册 server → client notification 回调
  - `def close(self) -> None`：清理资源
  - `def is_alive(self) -> bool`：底层是否健康
- 公共并发 routing 逻辑放在 base：`self._pending: dict[int, Future[Response]]`、`self._notification_callbacks: list[...]`、`self._reader_lock` 等

### `src/mcp/transport/stdio.py`（~100 LOC）
- `StdioTransport(command: list[str], env: dict[str, str] | None = None, cwd: str | None = None)`
- 实现细节按研究：
  - subprocess.Popen + stdin/stdout pipe + 后台 reader 线程
  - framing = newline-delimited JSON（每条消息独立一行，server 用 `\n` 终结）
  - reader 线程消费 stdout，按 JSON 解析；非 JSON 行 warn + 跳过（容忍启动 banner）
  - reader 异常 / EOF / `proc.wait()` 触发 → 所有 pending future `set_exception(MCPTransportError)`
  - close() 顺序：close stdin → wait(timeout=2) → terminate → wait(timeout=2) → kill
  - stderr 重定向到 subprocess.PIPE，单独后台线程消费（写到 logger.warning）

### `src/mcp/transport/http_legacy.py`（~120 LOC）
- `HttpLegacyTransport(url: str, headers: dict[str, str] | None = None)`：MCP 2024-11-05 双端点
- start()：发 GET `url` 建立 SSE 流；首个 SSE event 应为 `event: endpoint, data: <POST_URL_string>`，记下 POST URL
- send()：POST 到 endpoint URL，body 是 JSON-RPC 消息（一次一条）
- SSE 流后续 `event: message` 事件携带 response / notification
- 必带 header：`Accept: text/event-stream`（GET）、`Content-Type: application/json`（POST）、`MCP-Protocol-Version: 2024-11-05`
- 用户配置的 headers 合并进去（用户 headers 不覆盖协议必需的）

### `src/mcp/transport/http_streamable.py`（~150 LOC）
- `HttpStreamableTransport(url: str, headers: dict[str, str] | None = None)`：MCP 2025-03-26 单端点
- start()：可选 GET 建立 SSE listening 流（用于 server-to-client notification）；也可能服务器不支持单独 GET，需 try-and-fallback
- send()：POST 到 `url`；response 可能是 `Content-Type: application/json`（单条响应）或 `text/event-stream`（流式响应或多条消息）
- 解析 `Mcp-Session-Id` 响应头并在后续请求带上
- 必带 header：`Accept: application/json, text/event-stream`（POST）、`MCP-Protocol-Version: 2025-06-18`

### `src/mcp/_sse.py`（~40 LOC）
- 内部模块，纯函数式 SSE 解析器
- `parse_sse_stream(lines: Iterable[str]) -> Iterator[dict[str, str]]`
- 按 WHATWG 规范实现：处理 `event:` / `data:` / `id:` / `retry:` / 注释 `:` / 多行 data 拼接 / 空行作分隔
- 不实现 Last-Event-ID 重连（v1 留待 hardening PR）

### `src/mcp/config.py`（~80 LOC）
- `@dataclass(slots=True) MCPServerConfig`：含 name / transport / command / args / env / url / headers / start_timeout
- `@dataclass(slots=True) MCPConfig`：含 servers (list)、max_result_text_bytes、max_result_binary_bytes、start_timeout（全局默认）
- `parse_mcp_config(raw: dict) -> MCPConfig`：从 `[mcp]` + `[[mcp.servers]]` 段解析；每个 server 按 `transport` 字段校验必填字段（stdio 需 command；http_* 需 url）
- raise `MCPError`（来自 errors.py）on 配置错误

### `src/mcp/__init__.py`
- 导出公共 API：`Transport`, `StdioTransport`, `HttpLegacyTransport`, `HttpStreamableTransport`, `Request`, `Response`, `Notification`, `MCPError`, `MCPTransportError`, `MCPProtocolError`, `MCPServerConfig`, `MCPConfig`, `parse_mcp_config`

### 测试 `tests/test_mcp_*.py`
- `test_mcp_protocol.py`：encode/decode round-trip、ErrorObject、id 单调递增、不支持 batch
- `test_mcp_sse.py`：单事件、多行 data 拼接、注释跳过、空行分隔、BOM 容忍
- `test_mcp_transport_stdio.py`：用 mock subprocess（启动一个 python 子进程跑 echo server 脚本）验证 request/response 往返、并发 5 个 request 正确路由、subprocess 死亡时所有 pending 报 `MCPTransportError`、stderr banner 不打断
- `test_mcp_transport_http_legacy.py`：用 `pytest-httpserver` 或自写最小 `http.server` 验证 endpoint 协商 + POST + SSE response
- `test_mcp_transport_http_streamable.py`：同上但单端点
- `test_mcp_config.py`：合法配置、缺失必填字段抛错、stdio + http 各一示例

## Acceptance Criteria

- [ ] `src/mcp/__init__.py` 导出列出的全部公共 API
- [ ] `from src.mcp import StdioTransport` 不报错；构造一个 echo server 子进程能跑通 request/response 往返
- [ ] 三个 Transport 实现都通过 `Transport` ABC 的 isinstance 检查
- [ ] `parse_mcp_config(raw_toml_dict)` 解析示例配置（stdio + http_legacy + http_streamable 各一）成功
- [ ] 测试覆盖：并发 routing、subprocess 死亡、SSE 多行 data、协议消息编解码
- [ ] `ruff check src/mcp tests/test_mcp_*.py` 全绿
- [ ] `pytest tests/test_mcp_*.py` 全绿

## Definition of Done

- 不破坏任何现有测试（`pytest` 全集合 green）
- 新增模块行数控制在 ~600 LOC（不含测试）
- 所有模块顶部 `from __future__ import annotations`
- 公共 API 全部有类型注解
- 不修改 `src/core/` / `src/main.py` / `src/provider/`（只新增 `src/mcp/`）
- httpx 已是 anthropic SDK 的传递依赖；如果发现不是，pyproject.toml 显式声明

## Out of Scope（推到 PR2-6）

- 任何 `MCPClient` / `MCPManager` 类（PR2）
- MCP 握手（initialize / initialized）（PR2）
- tool/resource/prompt schema 注入 BareAgent 工具列表（PR2-3）
- 与 `PermissionGuard` / `agent_types.py` 的集成（PR4）
- 多内容块结果 / image / audio（PR5）
- 启动超时、payload 截断、atexit 清理（PR6）
- Last-Event-ID 重连（PR6）
- HTTP OAuth flow（v2）

## Technical Notes

- 父任务 PRD 见 `../05-27-mcp/prd.md`
- 关键研究：
  - `../05-27-mcp/research/json-rpc-edge-cases.md` — stdio framing、并发路由、进程死亡侦测
  - `../05-27-mcp/research/sse-parsing-minimal.md` — SSE 解析 + MCP HTTP 双版本
  - `../05-27-mcp/research/mcp-protocol-spec.md` — initialize / tools / resources 完整 schema（PR1 用不到 method 细节但 protocol 层概念要懂）
  - `../05-27-mcp/research/popular-mcp-servers.md` — server 启动行为参考
- 现有可借鉴模式：
  - `src/concurrency/background.py` — daemon 线程管理
  - `src/core/handlers/bash.py` — subprocess + timeout
- 必须遵循 `.trellis/spec/backend/` 的所有约定，尤其：
  - `logging-guidelines.md`：不用 `print()`，warn 走 `_log.warning(...)` 模式（已有少数模块用 `logging.getLogger(__name__)`，可参考）
  - `error-handling.md`：自定义异常、边界处校验
  - `state-persistence.md`：MCP 配置进 `MCPConfig` dataclass，不持久化（运行时状态）
  - `quality-guidelines.md`：Python 3.12+ 特性 + ruff + 类型注解全覆盖
