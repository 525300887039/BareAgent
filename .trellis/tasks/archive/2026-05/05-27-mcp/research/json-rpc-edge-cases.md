# JSON-RPC + MCP stdio Edge Cases — Research Notes

> Scope: 工程化实现 BareAgent 内的 MCP stdio client transport 时必须正确处理的协议边界。
> 参考规范：JSON-RPC 2.0、MCP `2025-06-18`（当前稳定）、`mcp-python-sdk` `src/mcp/client/stdio.py`。

---

## id 路由 / 并发请求

JSON-RPC 不保证 response 顺序——服务端可以并发处理，按任意顺序回写。客户端必须**根据 `id` 字段把 response 派发回正确的等待者**。

### 推荐模式：`id -> Future` 字典

```python
import asyncio, itertools, json
from typing import Any

class StdioJsonRpcClient:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self._next_id = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, callable] = {}
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: dict | None = None) -> Any:
        msg_id = next(self._next_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)
        try:
            return await asyncio.wait_for(fut, timeout=60)
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)  # 注意：无 id

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            # 这是一个 response
            fut = self._pending.get(msg["id"])
            if fut is None or fut.done():
                return  # 孤儿响应：忽略或日志
            if "error" in msg:
                fut.set_exception(JsonRpcError(msg["error"]))
            else:
                fut.set_result(msg["result"])
        elif "method" in msg:
            # 来自 server 的 request 或 notification
            handler = self._notification_handlers.get(msg["method"])
            if handler:
                await handler(msg.get("params"))
            # server-to-client request 需要回写 response（含 msg["id"]）
```

### 注意事项

- **id 必须唯一且未被复用**——使用单调递增计数器最安全。规范允许 string/number/null，但 `null` 在大多数实现里仅用于 parse-error 响应，不要主动用作请求 id。
- **取消 / 超时**：调用者超时后必须从 `_pending` 中弹出 future。否则迟到的 response 会触发 `InvalidStateError`。配合 MCP `notifications/cancelled` 通知告知 server 已放弃。
- **孤儿响应**：server 可能在 client 已 abandon 后才回——直接丢弃，不要崩溃。
- **进程死亡**：reader 退出时需把所有 pending future 全部 `set_exception(ConnectionClosed)`，否则调用方永远 hang。
- **避免重入死锁**：handler 内部不要再 `await self.request(...)` 同时阻塞读循环；handler 应 `asyncio.create_task()` 起子任务。

---

## 标准错误码

| Code              | 含义                | 谁产生  | BareAgent 客户端推荐处理                          |
| ----------------- | ----------------- | ---- | ------------------------------------------- |
| `-32700`          | Parse error       | 服务端  | 表示我们发出的字节流不是合法 JSON——本端 bug，记日志并断开重连        |
| `-32600`          | Invalid Request   | 服务端  | 我们 payload 结构有问题（缺 `jsonrpc:"2.0"` 等）——本端 bug |
| `-32601`          | Method not found  | 服务端  | 工具调用前先在 `initialize` 阶段检查 capabilities；运行期触发则当作 tool 错误返回给 LLM |
| `-32602`          | Invalid params    | 服务端  | 参数不符合 schema——把 message 透传回 LLM 让它修正        |
| `-32603`          | Internal error    | 服务端  | 服务端内部异常，向用户展示原 message，不重试                  |
| `-32000`~`-32099` | Server error（保留段） | 服务端  | 实现自定义；MCP 用此段表达业务错误，按 message 文本透传          |
| `-32099`~`-32000` 外，应用层错误 | —    | 任意 | 不应出现于此区间；其它负数留给应用                     |

> **客户端自身产生错误时**（例如收到的 response 缺字段）走本地异常，不要伪造 -32xxx 码。仅在我们也作为 server 角色（响应 server-to-client request）时才回写 error object。

---

## Batch requests

- **JSON-RPC 2.0 允许**：client 可发送 request 数组，server 按数组回 response（notification 不回）。
- **MCP `2025-06-18` 明确移除了 batch 支持**（PR #416），原因是 streamable-HTTP 实施时未发现有价值用例，且简化了 SDK。当前所有主流 MCP server 不应再发或接受 batch。
- **实现建议**：BareAgent 的 transport 层**不实现 batch**——发送端始终一条一行；接收端如果遇到顶层 JSON Array，直接返回 `-32600` 等价的本地错误并记日志。这与 mcp-python-sdk 当前行为一致（issue #934 也确认 SDK 主动拒收 batch）。

---

## Notification 处理

Notification = **无 `id` 字段的 request**，服务端/客户端**禁止回复**。MCP 中典型的 notification：

### client → server

- `notifications/initialized`：完成 initialize 握手后发送
- `notifications/cancelled`：取消一个已发出的 request（携带原 `requestId`）
- `notifications/progress`：罕见

### server → client

- `notifications/tools/list_changed`：工具列表变更，client 应重新调用 `tools/list`
- `notifications/resources/list_changed` / `notifications/resources/updated`
- `notifications/prompts/list_changed`
- `notifications/message`：日志消息（level + data）

### 实现要点

- **派发**：若入站消息有 `method` 字段，**有 id** = server-to-client request（必须回复），**无 id** = notification（不回复）。
- **list_changed 处理**：维护一个 `tools_cache_dirty` 标志，下一次 LLM 需要 tool list 时再 lazy refresh，避免在读循环里同步发请求造成自死锁。
- **未知 method**：notification 直接忽略；server-to-client request 回写 `-32601`。

---

## stdio framing

**MCP 采用 newline-delimited JSON（NDJSON），不使用 LSP 风格的 `Content-Length` header**。规范原文：

> Messages are delimited by newlines, and **MUST NOT** contain embedded newlines.

即：每条消息一行，`json.dumps` 默认就不带换行（紧凑模式更稳），写入后追加 `\n`。读端按 `\n` 切分，跨 chunk 用 buffer 拼接。

### 标准 framing 代码示例

```python
# 写端：保证单行
async def _write(self, payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert "\n" not in line, "JSON-RPC message must not contain embedded newlines"
    self.proc.stdin.write((line + "\n").encode("utf-8"))
    await self.proc.stdin.drain()

# 读端：buffer + split('\n')
async def _read_loop(self) -> None:
    buffer = ""
    try:
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break  # EOF：进程死亡或关闭了 stdout
            buffer += chunk.decode("utf-8", errors="replace")
            *lines, buffer = buffer.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 关键：MCP 规范要求 server 不写非 JSON 到 stdout
                    # 但实践中很多 server 在启动期会打印 banner，须容忍
                    logger.warning("non-JSON line on stdout: %r", line[:200])
                    continue
                await self._dispatch(msg)
    finally:
        self._fail_pending(ConnectionClosed("stdout EOF"))
```

> UTF-8 是规范要求；`errors="replace"` 避免单字节损坏让整个 reader 崩溃。

---

## 健壮性

### 过滤 server 启动日志 / 误输出

- 规范：server **MUST NOT** 写非 MCP 消息到 stdout，日志应走 stderr。
- 现实：大量 Node/Python 实现的 server 会在启动期打印 banner、warning、deprecation。
- 策略：
  1. 收到无法解析为 JSON 的行 → `logger.warning`，**不要断开连接**。
  2. 收到合法 JSON 但不是 JSON-RPC（无 `jsonrpc:"2.0"` 字段）→ 同样 warn 并跳过。
  3. stderr 直接转发到本进程 stderr 或单独的日志文件；**绝不**当作协议数据解析。

### 检测半截输出 / 进程死亡

- **EOF**：`proc.stdout.read()` 返回 `b""` → 进程关闭了 stdout，视为终止。
- **退出码**：启动 reader 同时启动 `await proc.wait()` 守望任务；任一完成就触发清理。
- **写失败**：`stdin.write` 抛 `BrokenPipeError` / `ConnectionResetError` → 进程已死。
- **半截 line**：buffer 中残留无换行的尾部数据时进程死亡 → 直接丢弃，记 warning（"truncated message at shutdown"）。
- **悬挂 pending**：reader 终止 / `proc.wait()` 完成时，遍历 `_pending` 全部 `set_exception(ConnectionClosed)`。
- **优雅关闭**：依次 `stdin.close()` → 等 ≤2s → `proc.terminate()` → 再等 ≤2s → `proc.kill()`（参考 mcp-python-sdk `PROCESS_TERMINATION_TIMEOUT = 2.0`）。
- **Windows**：用 Job Object 防止子进程的孙进程残留。

---

## 对 BareAgent 实现的启示

1. **`transport.py` 用 NDJSON + asyncio.subprocess**，不要引入 LSP-style header；写端 `json.dumps(..., separators=(",", ":"))` 强制单行 + 追加 `\n`，读端按 `\n` 切并维护 buffer。
2. **路由层用 `dict[int, Future]`**，请求方 `await future`；reader 协程负责派发 + EOF 时清空 pending 全部 set_exception，避免上层永久挂起。
3. **不实现 / 不发送 batch**——按 MCP `2025-06-18` 规范。顶层数组消息当协议错误日志后丢弃。
4. **明确区分四类入站消息**：`id+result/error` → 路由到 future；`method+id` → server-to-client request（回 `-32601` 暂不支持）；`method` 无 id → notification（按 method 分发，未知则忽略）；其他 → warn 丢弃。
5. **stderr 与非 JSON stdout 都不应中断连接**：stderr 直接重定向到本进程 stderr 或 logger；stdout 上的非 JSON 行只 warn。`tools/list_changed` 等 notification 仅置脏标志，懒刷新避免读循环内同步请求。

---

## 来源

- JSON-RPC 2.0 Specification — <https://www.jsonrpc.org/specification>
- MCP Spec `2025-06-18` / Transports — <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- MCP `2025-06-18` Changelog（移除 batching）— <https://modelcontextprotocol.io/specification/2025-06-18/changelog>
- PR #416 "Remove batching requirement" — <https://github.com/modelcontextprotocol/modelcontextprotocol/pull/416>
- mcp-python-sdk stdio client — <https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/client/stdio.py>
- Issue #934 "Python SDK rejecting JSON-RPC batch" — <https://github.com/modelcontextprotocol/python-sdk/issues/934>
