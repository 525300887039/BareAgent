# PR6: MCP 生命周期硬化 + E2E + 文档

## Goal

父任务 `05-27-mcp` 的收尾 PR。把 MCP 客户端从「能用」推进到「随系统稳定运转」：

- 子进程 / 远端异常被 **主动** 感知 → 自动标 UNHEALTHY 并推送 REPL 通知（不靠下次调用才发现）
- 退出路径无 MCP 僵尸进程（正常退出 / Ctrl+C / SIGTERM 都兜得住）
- 单次 tool result payload 有边界（text 256 KiB、binary 5 MiB），不会因某次大响应占爆 LLM 上下文
- OpenAI Responses-API 路径 image 投递补齐（PR5 trellis-check 遗留）
- 真实 `mcp-server-fetch`（uvx）跑通 stdio 全流程
- `CLAUDE.md` / `directory-structure.md` / `config.toml` 文档同步

完成后父任务 14 项 AC 全部勾选。

## Requirements

### 1. 进程崩溃 → UNHEALTHY 推送（Proactive 链路）

**Transport 层**
- `Transport` ABC 增加 `set_disconnect_handler(cb: Callable[[str], None]) -> None`（用 setter，不改 `__init__` 签名）
- `StdioTransport` reader 线程检测到 EOF / `BrokenPipeError` / 子进程 returncode 非 None → 调用 disconnect handler（仅当是 unexpected，graceful close 跳过）→ 再走 `_fail_all_pending`
- `HttpLegacyTransport` / `HttpStreamableTransport` SSE reader 在连接断流 / httpx 异常时同等触发
- 必须区分 **graceful close**（用户调用 `close()`）和 **unexpected**（reader 自己感知）：graceful 不触发 disconnect handler。建议在 `close()` 里设一个 `_closing = True` flag，handler 触发前检查。

**Manager 层**
- `_build_client` 后立刻 `transport.set_disconnect_handler(lambda reason: self._on_disconnect(server.name, reason))`
- `_on_disconnect(name, reason)`：
  - 加锁标 `_status[name] = UNHEALTHY`、`_clients.pop(name, None)`
  - `console.print_error(f"MCP server {name!r} disconnected: {reason}")`
  - 通过 `NotificationManager`（如已注入）推送一条 background-style notification — 让用户在异步上下文也能感知

**注入路径**
- `MCPManager` 构造参数新增 `notifier: NotificationManager | None`
- `src/main.py` 把现有 NotificationManager 实例传进 MCPManager

### 2. atexit + SIGTERM 兜底清理

`src/main.py::main` 在 `MCPManager` 构造后立刻：

```python
atexit.register(manager.close_all)

def _sigterm_handler(_signum, _frame):
    sys.exit(130)  # 让 atexit 跑到

signal.signal(signal.SIGTERM, _sigterm_handler)
```

- **不接管 SIGINT**：prompt-toolkit / KeyboardInterrupt 链路已有现有处理；不要重复注册
- `atexit.register(manager.close_all)` 同时让正常退出 / `sys.exit()` / SIGTERM handler 走同一清理路径
- `StdioTransport.close()` 现已 `proc.terminate()` + `proc.wait(timeout=...)`，保留；这里只是兜底触发

### 3. Payload 截断接线

**配置**
- `config.py::_DEFAULT_MAX_TEXT_BYTES` = `262_144`（256 KiB；当前是 1 MiB 错的）
- `_DEFAULT_MAX_BINARY_BYTES` 保持 `5_242_880`（5 MiB，已对）

**接线点 = `src/mcp/registry.py`**
- `_to_content_blocks(content, mcp_config)` 接受配置参数（或经由 client → manager → registry 取到 `MCPConfig`）
- **text content** 字节长度 > `max_result_text_bytes` → 截断到上限 + 追加 `\n[truncated, original size: N bytes]`
- **image / embedded_resource binary**（base64 decode 后的字节长度）> `max_result_binary_bytes` → 替换为 text content `[Resource omitted: too large (N bytes)]`
- `_flatten_content`（error path）同样对 text 应用截断 — 错误信息也不应无界

**契约**
- 截断只发生在 registry 层（content normalization 边界），provider 层照常 passthrough
- LLM 看到的是带 `[truncated, ...]` / `[Resource omitted: ...]` 后缀的明文，能感知 → 必要时可改参数重试

### 4. OpenAI Responses-API 多模态适配（PR5 遗留）

`src/provider/openai.py::_convert_response_message`（line 327）当前 stringify `list[dict]`。改为：

- 当 `tool_result.content` 是 `list[dict]` 且含 image block 时，走和 `_convert_tool_result_for_openai`（chat_completions 路径）一样的 lift 逻辑：image 提升到紧跟的 user message，tool role message 留 text 部分（或 placeholder）
- 抽公共 helper `_lift_image_blocks(tool_use_id, content) -> tuple[str, list[image_block]]`，两个路径都用

### 5. mcp-server-fetch E2E 冒烟

**`tests/test_mcp_e2e_manual.py`**
- 使用 `uvx mcp-server-fetch` 启动 stdio server
- 测试用例：
  - `test_fetch_handshake`：manager.start_all → iter_running_clients 出现 `fetch`，capability 含 `tools`
  - `test_fetch_call`：调用 `mcp__fetch__fetch` 请求 `https://example.com`，验证 content 有非空 text block
- pytest mark 走文件名后缀 `_manual.py`（项目现有约定，CI 默认排除）
- 用例运行前 `pytest.importorskip` 或 `subprocess.run(["uvx", "--version"])` 检测，缺工具时 skip 而非 fail

### 6. 文档同步

**`CLAUDE.md`**
- 在 `## 架构` 章节新增小节：**MCP 客户端 (`src/mcp/`)**
  - 简述：MCPManager 管理多个 MCPClient；transport ABC（stdio / http_legacy / http_streamable）；registry 注入 `mcp__<server>__<tool>` 工具 + `mcp__<server>__resource_read`；slash 命令 `/mcp:<server>:<prompt>`；REPL 命令 `/mcp status|list|reload`
  - 关键文件：`src/mcp/__init__.py`、`src/mcp/manager.py`、`src/mcp/registry.py`、`src/mcp/client.py`、`src/mcp/transport/`
  - 配置：指向 `config.toml [mcp]` 段
  - 子代理隔离：`AgentType.mcp_tools_enabled`（read-only 类型默认 False）

**`.trellis/spec/backend/directory-structure.md`**
- top-level layout 加 `src/mcp/` 条目（管理器 + 客户端 + transport + protocol + registry + config + errors）
- decision tree 加：**新 MCP 集成相关？** → `src/mcp/`，按 transport / protocol / 多 server 协调 / schema 注入路由到对应模块

**`config.toml`**（项目默认 config）
- 加注释示例 `[[mcp.servers]]`（stdio + http_streamable 各一）
- 列 `[mcp] start_timeout` / `max_result_text_bytes` / `max_result_binary_bytes` 默认值

## Acceptance Criteria

- [ ] kill 一个 stdio MCP 子进程 → 控制台立刻（≤ 200ms）出现 `MCP server X disconnected: ...` 通知；`/mcp status` 显示 unhealthy；下一次 LLM 调用看不到该 server 的 `mcp__X__*` 工具
- [ ] BareAgent 退出（正常 / Ctrl+C / SIGTERM）后 `ps`（Linux/macOS） / `tasklist`（Windows）查不到 MCP 子进程残留
- [ ] `config.py::_DEFAULT_MAX_TEXT_BYTES == 262_144`
- [ ] registry 截断：250 KiB text → 通过；257 KiB text → 命中截断 + LLM 收到 `[truncated, original size: 263168 bytes]` 后缀
- [ ] registry 截断：6 MiB base64 image → 替换为 `[Resource omitted: too large (...)]` 占位 text
- [ ] OpenAI Responses-API 路径接收到含 image 的 tool_result → 模型下一轮能看到 image
- [ ] `tests/test_mcp_e2e_manual.py` 在本地 uvx 环境跑通（无 uvx 时 skip 而非 fail）
- [ ] `CLAUDE.md` / `.trellis/spec/backend/directory-structure.md` / `config.toml` 文档同步
- [ ] 所有现有 pytest 全绿（PR1-PR5 累计 ≥ 412 个 case 不退化）；ruff check + format 全绿
- [ ] 新增测试 ≥ 8 个：on_disconnect 触发（stdio）+ on_disconnect 区分 graceful/unexpected + text 截断临界 + binary 占位 + flatten_content 截断 + atexit 注册 + Responses-API image lift + summarize 反映 UNHEALTHY 状态

## Definition of Done

- 上述所有 AC 勾选
- 父任务 05-27-mcp 14 项 AC 全部勾选；如全绿则在 `/trellis:finish-work` 时考虑把父任务一并 archive
- `Feat：` 前缀 commit；commit message 总结 6 项硬化点
- 新沉淀 spec（如适用）：on_disconnect 区分 graceful/unexpected 的约定如果有可复用价值，写入 `.trellis/spec/backend/error-handling.md`；payload 截断"在 normalization 边界做"如果有可复用价值，写入 `directory-structure.md`

## Out of Scope

- **Per-server / per-tool payload 阈值**：v1 全局
- **断流自动重连**：用户手动 `/mcp reload`
- **last-event-id 续传** （SSE）：留 ROADMAP
- **OAuth flow**：仍是 Bearer
- **配置热重载**：编辑 `config.toml` 不重启的能力 → ROADMAP 4.3
- **Sampling / elicitation / roots**：client 不暴露 server-callable capability
- **父任务整体 PR 链路（PR1-PR6）的回归 E2E suite**：单独走 manual 测试，本 PR 不引入 framework 级别的 E2E 体系

## Technical Approach

### on_disconnect 触发链

```
StdioTransport reader thread:
  while True:
    line = self._proc.stdout.readline()
    if not line:                     # EOF
      if not self._closing:           # 区分 graceful
        self._invoke_disconnect("stdout EOF (subprocess exited)")
      self._fail_all_pending(...)
      return
    ... parse + route ...
```

Manager 端：

```python
def _build_client(self, server):
    transport = self._construct_transport(server)
    transport.set_disconnect_handler(
        lambda reason: self._on_disconnect(server.name, reason)
    )
    return MCPClient(server, transport)

def _on_disconnect(self, name, reason):
    with self._lock:
        self._status[name] = ServerStatus.UNHEALTHY
        client = self._clients.pop(name, None)
    msg = f"MCP server {name!r} disconnected: {reason}"
    if self._console:
        self._console.print_error(msg)
    if self._notifier:
        self._notifier.notify(msg)
```

### Truncation 实现位置

- `_to_content_blocks` 在转 image / text 时检查 size — image 在 base64 decode 之前 / 之后？
  - **决定**：检查 base64 字符串的 `len(data) * 3 / 4` 估算 byte size（避免 decode 大 payload 占内存）；超限 → 不 decode，直接替换为占位文本
- text 检查 `len(text.encode("utf-8"))`（utf-8 字节长度，与 `max_result_text_bytes` 同语义）

### atexit + signal 注册时序

`src/main.py::main`：

```python
manager = MCPManager(mcp_config, console=ui_console, notifier=notification_manager)
manager.start_all()
atexit.register(manager.close_all)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
# ... agent_loop ...
```

注：`atexit` handler 在 `manager.close_all` 已经被 finally 块调用过的情况下应保持幂等（现 `close_all` 已加锁且 clear `_clients`，是幂等的）

### Responses-API lift helper

提取 `_lift_image_blocks(tool_use_id, content_blocks) -> tuple[str | list, list[dict]]`：
- 输入：tool_use_id + content blocks
- 输出：`(non-image content for tool role, image blocks for follow-up user message)`
- `_convert_tool_result_for_openai`（chat_completions）+ `_convert_response_message`（Responses-API）共享

## Decision (ADR-lite)

**Context**：MCP 客户端 PR1-PR5 已经把"happy path"和"权限/多模态"打通，但缺生命周期硬化和文档同步。如果不做就交付的话，子进程异常无法即时感知（lazy），退出可能留僵尸，单次大响应可能炸 LLM context，新人读 codebase 不知道 `src/mcp/` 干啥。

**Decision**：在一个 PR 里集中收尾 6 项，不再拆 — 都是小改动，且互相是同一主题（生命周期 + 文档）。

**Consequences**：
- ✅ 父任务 05-27-mcp 完整闭环，可 archive
- ✅ 后续 MCP 相关扩展（v2 OAuth / sampling / per-server 阈值）有清晰的扩展点
- ⚠️ PR diff 比单个原子改动大，但每项都有 AC 对应，trellis-check 时能逐项验

## Technical Notes

- on_disconnect 的 graceful/unexpected 区分如果未明确处理，会导致 `close_all()` 后弹出"disconnected"通知（噪声）→ 实现时务必加 `_closing` flag 类机制
- `NotificationManager` 现有用于 background task 完成通知，复用同一通道即可；不要为 MCP 单独建一套
- mcp-server-fetch 在 `https://example.com` 这种公网请求时可能慢；E2E 测试给个宽松 timeout（30s 起）
- `_DEFAULT_MAX_TEXT_BYTES` 改默认值是 breaking change for 配置文件未显式声明的用户 — 但 256 KiB 是 LLM context 友好的合理上限，且父 PRD 明确要求；视为对齐 PRD 而非 regression
- 父任务 archive 时把 archive directory 落在 `.trellis/tasks/archive/2026-05/` 下，与 PR2-PR5 同目录

## Research References

（无新 research；沿用父任务 `.trellis/tasks/05-27-mcp/research/` 4 份现成材料）
