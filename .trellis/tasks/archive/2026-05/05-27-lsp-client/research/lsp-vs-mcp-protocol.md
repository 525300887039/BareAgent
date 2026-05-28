# Research: LSP 协议关键细节 vs BareAgent 现有 MCP 实现

- **Query**: 调研 LSP 协议关键细节 + 与 MCP 的差异，决定 LSP 实现是 “提取抽象共享” 还是 “独立新建”
- **Scope**: 外部 (LSP 3.17 规范 + 各 language server 文档) + 内部 (`src/mcp/*`)
- **Date**: 2026-05-27

---

## TL;DR

1. **Framing 完全不同**：LSP 用 HTTP 风格的 `Content-Length` header + `\r\n\r\n` + JSON body；MCP stdio 用裸 NDJSON。两者的 reader/writer 不能直接复用。
2. **JSON-RPC envelope 大体一致，但 LSP id 允许 `int | string`，MCP（`src/mcp/protocol.py:154`）强制 int** — `decode_message` 的 id 校验必须放宽才能复用。
3. **真正分叉点是 server→client request**：LSP 必须实现 `workspace/applyEdit` / `window/showMessageRequest` / `client/registerCapability` / `window/workDoneProgress/create` 这些反向请求并回 response；BareAgent 现在的 `StdioTransport._dispatch`（[src/mcp/transport/stdio.py:200](../../../../src/mcp/transport/stdio.py)）直接 ignore server 请求。+ LSP 有 `$/cancelRequest`、`$/progress` 这类协议级 notification 也是 MCP 没有的。结论：**JSON-RPC envelope + id 路由这一薄层可以共享，framing / lifecycle / 反向请求分发必须各自实现**。

---

## 1. LSP framing 与 lifecycle 速查

### 1.1 Base Protocol — Framing

> 规范: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#baseProtocol>

每条消息 = header part + content part，header 与 content 之间用 `\r\n` 分隔。

```
Content-Length: <N>\r\n
Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n   # optional
\r\n
{"jsonrpc":"2.0","id":1,"method":"textDocument/hover", ...}
```

要点：

- `Content-Length` **必需**，单位是 **字节** (UTF-8 编码后的 byte length)，**不是字符数**。
- Header 用 ASCII 编码（含分隔的 `\r\n`）。
- Content 默认 UTF-8；旧规范用过 `utf8`（非 IANA 名称），实现上应把 `utf8` 当 `utf-8` 兼容。
- header 字段语法对齐 HTTP，name 和 value 用 `: `（冒号+空格）分隔。
- 两个连续 `\r\n` 永远紧接 content。

**对比 MCP**：stdio 用 newline-delimited JSON（`src/mcp/transport/stdio.py:87`，`line = message + "\n"`，且 [src/mcp/protocol.py:113](../../../../src/mcp/protocol.py) 防御性检查 encoded JSON 内不含 `\n`）。LSP 的 framing 必须按字节读 `Content-Length` 个 byte，**不能** 用 `readline()` 这种按 `\n` 切分的方法。

### 1.2 Base Protocol — JSON-RPC envelope

> 规范: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#abstractMessage>

LSP 完全用 JSON-RPC 2.0。差异点（vs `src/mcp/protocol.py`）：

| 字段 | LSP 3.17 | BareAgent MCP (protocol.py) |
|---|---|---|
| `id` (request) | `integer \| string` | **`int` only** (`src/mcp/protocol.py:154`) |
| `id` (response) | `integer \| string \| null` | `int \| None` (`src/mcp/protocol.py:163`) |
| `params` 类型 | `array \| object`（请求/通知都允许 array） | 仅 `object`（`src/mcp/protocol.py:150`） |
| 批量 / batch | 规范没说禁止，但实践上几乎没人用；vscode-jsonrpc 不发 batch | **显式拒绝** (`src/mcp/protocol.py:129`，MCP 2025-06-18 移除了 batch) |
| 标准错误码 | `-32700…-32603`、`-32099…-32000`、+ LSP 专用 `-32899…-32800` (`RequestFailed=-32803` / `ServerCancelled=-32802` / `ContentModified=-32801` / `RequestCancelled=-32800`) + `ServerNotInitialized=-32002` | 仅 JSON-RPC 标准码 + `-32099…-32000` server error 段 |
| `$/`-前缀 method | 协议层杂项（cancel/progress/setTrace/logTrace），收到不认识的 `$/xxx` notification 可以静默忽略；`$/xxx` request 必须回 `MethodNotFound` | 不存在 |

> 规范: 错误码区间 <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#errorCodes>；`$/` 约定 <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#dollarRequests>

### 1.3 Lifecycle — initialize → initialized → shutdown → exit

> 规范: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#lifeCycleMessages>

四阶段（粗体 = 实现必须做的）：

1. **`initialize` (request, client→server)**：必须是第一条 request。`InitializeParams` 关键字段：
   - `processId: integer | null` — 父进程 PID，**server 检测到该进程不在了应自行 exit**（这是 LSP 的死亡检测机制，BareAgent 实现需要传自己的 PID）。
   - `rootUri: DocumentUri | null` (已废弃，留兼容) / **`workspaceFolders: WorkspaceFolder[] | null`** (3.6+，主推) / `rootPath` (已废弃)。
   - `clientInfo: { name, version? }` — 类比 MCP 的 `clientInfo`。
   - `capabilities: ClientCapabilities` — **必填**，按 `textDocument.<feature>.dynamicRegistration` / `workspace.applyEdit` / `window.workDoneProgress` 等键宣告自己支持的方法。
   - `initializationOptions: LSPAny` — server 自定义启动配置（pyright 用它传 `python.pythonPath` 等设置）。
   - `trace: 'off' | 'messages' | 'verbose'`（可选）。
   - `locale: string`（3.16+，IETF 语言标签）。
2. **`initialize` response**：返回 `InitializeResult { capabilities: ServerCapabilities; serverInfo?: { name, version? } }`。
   - 收到 response **之前**，client 不得发任何其他 request/notification；server 也不得发，**例外**：`window/showMessage`、`window/logMessage`、`telemetry/event` notification，以及 `window/showMessageRequest` request；如果 client 在 `InitializeParams` 设置了 `workDoneToken`，server 还能用 `$/progress`。
   - error code `-32002 ServerNotInitialized`：server 收到 init 之前的 request 必须用这个回。
3. **`initialized` (notification, client→server)**：client 收到 init response 后、发任何其他消息前，发一次（且只发一次）。server 可借此触发 dynamic capability registration。
4. **`shutdown` (request, client→server)**：要求 server 准备退出但**不要 exit**（否则 response 投递不及）。`result: null`。shutdown 之后 client 不得再发任何 request（除非是 `exit` notification）。
5. **`exit` (notification, client→server)**：让 server 退出进程。收到过 `shutdown` 后 server 以 exit code 0 退；没收到过 `shutdown` 直接 exit 应该用 code 1。

**与 MCP 对比**：MCP `2025-06-18` 的 lifecycle 是 `initialize` request → `notifications/initialized` notification，**没有显式的 shutdown/exit** —— MCP 直接关 stdio 即可，BareAgent 在 `src/mcp/client.py:111` 也只发 `notifications/initialized`，关闭走 `StdioTransport.close()` 的 stdin close → terminate → kill（[src/mcp/transport/stdio.py:97](../../../../src/mcp/transport/stdio.py)）。**LSP 必须实现完整的 shutdown 握手** —— 不能简单复用 MCP 的 close 路径。

### 1.4 Capability negotiation

- **静态**：client 在 `InitializeParams.capabilities` 里声明它支持哪些 client capability（嵌套对象，按 `textDocument.<method>` / `workspace.<feature>` / `window.<feature>` 组织）；server 在 `InitializeResult.capabilities` 里回应它能提供哪些 feature（`ServerCapabilities`，键如 `hoverProvider`、`definitionProvider`、`textDocumentSync`、`diagnosticProvider`…）。
- **动态**：server 在 `initialized` 之后可以发 `client/registerCapability` / `client/unregisterCapability` request 增删能力，**前提是 client 在 init params 里对该 capability 声明了 `dynamicRegistration: true`**。client 必须返回 response（spec: `result: void`）。这是 server→client request。
- **MCP 对比**：MCP 的 `capabilities` 是扁平 `{tools, resources, prompts, logging}`，键存在即支持（BareAgent `src/mcp/client.py:204` `has_capability` 只看 key 是否在）；LSP 嵌套且有 sub-flag，需要按 `feature.subFeature` 取值。

### 1.5 协议版本

- **3.17** (2022) 是当前稳定主流。pyright、rust-analyzer、gopls、typescript-language-server 都至少实现到 3.17。
- **3.18** RFC 状态：spec 文档里少量字段标 `@since 3.18.0 @proposed`（如 `MessageType.Debug = 5`），不影响 MVP。
- LSP **没有** MCP 那种 `protocolVersion: "2025-06-18"` 字符串 — 版本协商靠 capabilities，没有显式 version handshake（spec 注 `unknownProtocolVersion: 1` `@deprecated` 自 3.0 起）。

---

## 2. LSP vs MCP 协议层差异矩阵（影响代码复用决策）

| 差异点 | MCP 现状 (BareAgent) | LSP 要求 | 影响 | 复用难度 |
|---|---|---|---|---|
| **Framing** | NDJSON, `message + "\n"`，按 `readline()` 切（[stdio.py:184](../../../../src/mcp/transport/stdio.py)） | `Content-Length: N\r\n\r\n` + N 字节 body | reader/writer 完全两套；不能用 `_iter_lines` | **不可复用** — 需要新写 byte-level reader |
| **id 类型** | `int` only ([protocol.py:154](../../../../src/mcp/protocol.py)) | `int \| string` (request) / `int \| string \| null` (response) | LSP server 实际上多数用 int（vscode-languageserver-node 用 number），但规范允许 string；强约束 int 会拒掉合法消息 | **小改可复用** — 放宽 `decode_message` 的 id 校验 |
| **`params` 类型** | `dict` only ([protocol.py:150](../../../../src/mcp/protocol.py)) | `object \| array` | LSP 实际 method 全部用 object，但 spec 允许 array | **小改可复用** — 放宽参数校验，或维持 object 限制（实践无碍） |
| **Batch 数组** | 显式拒绝 ([protocol.py:129](../../../../src/mcp/protocol.py)) | spec 不要求支持，主流 server 不发 | 现状即可 | **可复用** |
| **错误码** | 仅 JSON-RPC 标准 + server error 段 | + LSP 专用 `-32899…-32800`、`-32002 ServerNotInitialized` | 错误处理时需要识别 `ContentModified` / `RequestCancelled` / `ServerCancelled` / `RequestFailed`；其他逻辑不变 | **可复用** — 加一个 LSP 错误码常量集 |
| **`$/` 前缀语义** | 不存在 | `$/cancelRequest`、`$/progress`、`$/setTrace`、`$/logTrace` 等协议级杂项；未知 `$/` notification 必须忽略，未知 `$/` request 必须回 `MethodNotFound` | dispatch 层需要识别前缀 | **新增逻辑** |
| **Server→Client request** | reader 直接 ignore ([stdio.py:200](../../../../src/mcp/transport/stdio.py)) | **必须分发并回 response**（`workspace/applyEdit`、`window/showMessageRequest`、`window/showDocument`、`client/registerCapability` / `unregisterCapability`、`window/workDoneProgress/create`） | dispatch 模型从单向 (server→client only fires notifications) 变成双向 | **架构级新增** — 不可复用 |
| **Cancellation** | 无 | `$/cancelRequest` notification 取消未完成 request，被取消 request 仍需返回 response（建议错误码 `RequestCancelled = -32800`） | client 需要支持取消自己发出的 request；server 取消时识别错误码 | **新增** — `Transport.request` 需要可取消 |
| **Progress** | 无 | `$/progress` notification + token；`window/workDoneProgress/create` (server→client request) | 长操作要看进度；MVP 可以忽略，但 dispatch 至少不能崩 | **MVP 可暂缓**，但 dispatch 不能炸 |
| **Lifecycle close** | 关 stdin → wait → terminate ([stdio.py:97](../../../../src/mcp/transport/stdio.py)) | `shutdown` request → wait response → `exit` notification → wait exit code | 关闭流程更复杂；processId 死亡检测要求传我们自己的 PID | **新增 lifecycle 编排** |
| **Notification 方向** | 默认 server→client 单向 fanout ([base.py:111](../../../../src/mcp/transport/base.py)) | 双向，且某些 notification（如 `$/cancelRequest`）从 client→server | `Transport.notify` 已经支持双向写；接收侧不变 | **可复用** |
| **多 server 互调** | 一个 client 一个 server，1:1 | 同上 1:1 (`spec: 一个 server 一个 tool`) | 架构对齐 | **可复用** |
| **诊断推/拉模型** | 不存在 | 推：`textDocument/publishDiagnostics` notification（server→client，传统模型）<br>拉：`textDocument/diagnostic` request + `workspace/diagnostic` request（3.17 新增 `DiagnosticProvider`） | 两套都要看 server 声明的 `diagnosticProvider` 决定走哪条 | **新增** |
| **文本同步** | 不存在 | `textDocument/didOpen` / `didChange` (Full or Incremental) / `didClose` 必须支持；server 在 `textDocumentSync` 里声明用 Full 还是 Incremental | client 必须维护一个 “已 didOpen 的文档” 列表 | **新增** |
| **文件 URI** | 不存在 | `DocumentUri = string`，`file:///` scheme；**Windows 盘符大小写、`%3A` 编码各 client/server 不一致**（spec 警告：不要假设对方编码方式跟自己一样） | Windows 上跨 OS 兼容必须小心 | **新增** — URI helper |

> spec 文档对照锚点：
> - 错误码：[#errorCodes](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#errorCodes)
> - `$/`：[#dollarRequests](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#dollarRequests)
> - cancel：[#cancelRequest](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#cancelRequest)
> - progress：[#progress](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#progress)
> - register：[#client_registerCapability](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#client_registerCapability)
> - applyEdit：[#workspace_applyEdit](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#workspace_applyEdit)
> - showMessageRequest：[#window_showMessageRequest](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#window_showMessageRequest)
> - workDoneProgress/create：[#window_workDoneProgress_create](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#window_workDoneProgress_create)
> - publishDiagnostics / pull diagnostics：[#textDocument_publishDiagnostics](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#textDocument_publishDiagnostics)、[#textDocument_diagnostic](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#textDocument_diagnostic)
> - URI：[#uri](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#uri)

---

## 3. Server-initiated request 处理设计要点

LSP client 必须把 reader 收到的 `Request` 也走一次 method-dispatch（而不是像 MCP 那样直接 `_log.warning(...)` 丢弃）。每个方法分三类：

**A. 必须实现 happy-path 并回 response**（否则 server 会卡住或失败）：

- `client/registerCapability` → 接受注册（即使内部不真的支持也可以 `result: null` 接受，或者用 `error.code = -32601 MethodNotFound`；安全做法是 init params 不声明 `dynamicRegistration: true`，server 就不会发）。
- `client/unregisterCapability` → 同上。
- `window/workDoneProgress/create` → 如果 init params 没声明 `window.workDoneProgress = true`，server 就不会发；声明了就需要 `result: null` 接受 token。
- `workspace/configuration` (server 拉取 client 设置)：返回 client 已知的设置数组；不知道的项填 `null`。
- `workspace/workspaceFolders` (server 询问 folder 列表)：返回 init 时声明过的 folder 数组或 `null`。

**B. 可以拒绝（回 `MethodNotFound -32601`）但需要回 response**：

- `workspace/applyEdit` — agent 场景下可以直接拒（不让 server 改文件），但**必须**回 `{ applied: false, failureReason: "...not supported by agent client" }`，而不是 `MethodNotFound`，因为 spec 定义了这个 result shape。
- `window/showMessageRequest` — 直接回 `result: null`（“用户没选”）即可，server 应该容忍 null。
- `window/showDocument` — 回 `{ success: false }`。

**C. 必须主动忽略**（spec 明确说可忽略）：

- 任何未知的 `$/xxx` notification → 静默丢弃。
- 任何 `$/xxx` request → 回 `MethodNotFound -32601`。
- `telemetry/event` notification → 直接丢。

**分发器形状建议**：

```
dispatch(server_request) -> Response:
    if method in handlers: return handlers[method](params)
    if method.startswith("$/"): return error(MethodNotFound)
    return error(MethodNotFound)
```

handler 注册采用 `dict[str, Callable[[dict|None], Any]]`，复用 `src/mcp/transport/base.py:111` 的 fan-out 模式但加上 “必须回 response” 的语义；最关键的是 **`StdioTransport._dispatch` 里 `isinstance(msg, Request)` 那一支不能再 ignore**。

---

## 4. 多语言 server 启动配置速查表

> 验证渠道：各项目官方 README / docs。

| Server | 安装方式 | 启动命令 | 必备 args | initializationOptions / 备注 |
|---|---|---|---|---|
| **pyright** | `npm install -g pyright`（含 `pyright`+`pyright-langserver` 两个 bin） / 也有 PyPI `pip install pyright` 包装器 | `pyright-langserver --stdio` | `--stdio`（必填） | `pyrightconfig.json` / `pyproject.toml` 自动发现；通过 `workspace/configuration` 拉 `python.pythonPath`、`python.analysis.*`（见 <https://github.com/microsoft/pyright/blob/main/docs/settings.md>）。需要 Node >= 14。npm 包 `pyright`，bin 路径为 `pyright-langserver`（来自 `packages/pyright/package.json`）。|
| **rust-analyzer** | `rustup component add rust-analyzer` 或下载 GitHub release 二进制 / Arch AUR | `rust-analyzer` | 无（默认 stdio） | `initializationOptions` 可放 `rust-analyzer.*` 配置；不用 args 切 stdio。详见 <https://rust-analyzer.github.io/book/other_editors.html> |
| **typescript-language-server** | `npm install -g typescript-language-server typescript`（**必须同时装 `typescript`**，否则 server 起不来） | `typescript-language-server --stdio` | `--stdio`（必填） | 可选 `--log-level <1-4>`、`--tsserver-path`、`--tsserver-log-file`；`initializationOptions.preferences`、`initializationOptions.tsserver.path` 等。详见 <https://github.com/typescript-language-server/typescript-language-server> |
| **gopls** | `go install golang.org/x/tools/gopls@latest` | `gopls` (默认 LSP server over stdio) | 无 | 可选 `-rpc.trace`、`-logfile=<path>`、`-remote=auto`（daemon 模式，多 client 共享一个 gopls）；`initializationOptions` 用来传 `gopls.<setting>`。详见 <https://github.com/golang/tools/blob/master/gopls/doc/daemon.md> |

通用 `initialize` 模板（所有 4 个 server 都吃这个最小集）：

```jsonc
{
  "processId": <bareagent PID>,
  "clientInfo": { "name": "BareAgent", "version": "0.1.0" },
  "rootUri": "file:///workspace/abs/path",   // 兼容老 server
  "workspaceFolders": [
    { "uri": "file:///workspace/abs/path", "name": "workspace" }
  ],
  "capabilities": {
    "textDocument": {
      "synchronization": { "dynamicRegistration": false },
      "hover":      { "contentFormat": ["plaintext", "markdown"] },
      "definition": { "linkSupport": true },
      "publishDiagnostics": {}
    },
    "workspace": {
      "workspaceFolders": true,
      "configuration":   true   // 仅在你能回 workspace/configuration 时为 true
    },
    "window": {}                // 不声明 workDoneProgress 就不会收到 progress/create
  },
  "initializationOptions": null
}
```

各 server 都会基于 `capabilities` 自适应 —— 声明少 = 收到的反向请求/notification 也少（MVP 友好策略）。

---

## 5. 文件 URI / workspace 常见坑

- `file:///c:/...` vs `file:///C%3A/...`：**spec 明确警告**两个等价但不同形式都可能出现，client/server 不能假设对方与自己用相同编码 (spec [#uri](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification#uri))。Windows 下 BareAgent 当前 cwd 是 `D:\code\BareAgent`，需要 helper 把 Windows path 规范化成 `file:///D:/code/BareAgent`（注意：**第三个 `/` 后直接是盘符，不带 `:` 编码** 是最广兼容的形式；rust-analyzer / gopls / pyright 都能接受这种）。
- `workspaceFolders` 是数组，**单 folder 是常见配置**；为 `null` 表示 “没有打开 folder”，server 会进入 “single-file mode”，多数 feature 缺失（rust-analyzer 在没有 `Cargo.toml` 时退化得最明显）。
- `rootUri` 已 deprecated 但 LSP 3.6 之前的旧 server 还在看；**同时传 `rootUri` 和 `workspaceFolders` 最稳**。
- 文件外的文件（如 `node_modules/...` 里跳定义结果）会带 absolute 路径回来，client 要按 `file://` 解析后才能 read。

---

## 6. 抽象提层 vs 独立新建：建议

### 6.1 可以提到 `src/jsonrpc/` 共享的部分（提到一个新包，不动 `src/mcp/`）

把 [src/mcp/protocol.py](../../../../src/mcp/protocol.py) 拆成 **protocol-agnostic JSON-RPC 核心** + **MCP-specific 约束**：

- 共享：`ErrorObject`、`Request`、`Response`、`Notification` 数据类、`encode_message` (单行 JSON 序列化)、`new_request_id`、标准 JSON-RPC 错误码常量。
- 共享：`Transport` 基类的 **id 路由 + pending future + notification fanout + disconnect handler** 这一层（[src/mcp/transport/base.py](../../../../src/mcp/transport/base.py) 的核心机制和 framing 无关，**值得提层**）。
- 微调：`decode_message` 的 id / params 类型校验放宽，或暴露一个 strict 开关；MCP 在自己的 codec 层保留 `int-only`、batch-reject 等额外约束。

### 6.2 必须 LSP 独立写的部分

- **stdio framing**：byte-level `Content-Length` reader + writer（不能复用 `_iter_lines` 的按 `\n` 切）。建议放 `src/lsp/transport/stdio.py`，**不要**在 `src/mcp/transport/stdio.py` 里掺第二种 framing。
- **lifecycle**：`initialize` / `initialized` / `shutdown` / `exit` 四阶段编排（MCP 只有前两个）。
- **server-initiated request dispatcher**：MCP 没有，纯新增。
- **capability negotiation helper**：嵌套结构 + dynamicRegistration 判断，跟 MCP 的扁平 key 检查差太远。
- **textDocument 同步状态机**：维护打开文档表 + Full/Incremental 切换。
- **URI / Windows path 工具**。
- **诊断收集器**：兼容 push (`publishDiagnostics`) 和 pull (`textDocument/diagnostic`) 两套。

### 6.3 推荐目录形状

```
src/
  jsonrpc/                # 新建，protocol-agnostic
    __init__.py
    message.py            # Request/Response/Notification dataclass + encode/decode（可配 strict）
    errors.py             # JSON-RPC 标准错误码
    routing.py            # Transport 基类的 pending-future / fanout（不含 framing）
  mcp/                    # 保持不动，只是改 import 路径
    protocol.py           # 改为：import jsonrpc; 加 MCP 自己的 batch-reject 等约束
    transport/...
  lsp/                    # 全新
    protocol.py           # LSP 错误码、$/dispatch、capability 嵌套查询、URI helper
    lifecycle.py          # initialize/initialized/shutdown/exit 编排
    transport/
      stdio.py            # Content-Length framing
    handlers/             # server→client request handler 注册（applyEdit / showMessageRequest / ...）
    sync.py               # textDocument 同步状态机
    client.py             # 类比 src/mcp/client.py，但每个 server 一个
```

风险点：**为复用而复用容易把 `src/mcp/protocol.py` 改坏**。当前 MCP 实现里有针对 MCP 2025-06-18 的硬约束（int-only id、reject batch），如果改成 protocol-agnostic 然后 LSP 再传 strict flag，会增加 MCP 维护成本。建议 PR 顺序：**先独立写 `src/lsp/`，跑通 MVP（pyright + 1-2 个 method），然后再回头评估 `src/jsonrpc/` 抽层是否真的值得**。如果两边代码 99% 各自演化，那共享层就是负债。

---

## Caveats / Not Found

- 没逐一展开每个 method 的完整 schema（如 `Hover` / `Location` / `DocumentSymbol` 的字段），这是实现期再读的内容。
- 没有验证 4 个 server 的 **`workspaceFolders` 处理细节**（pyright 在多 folder workspace 下的 `pyrightconfig.json` 解析、rust-analyzer 多 crate workspace），需要 implement 期写最小启动脚手架去实测。
- LSP **没有原生认证机制**（stdio 直接信任 subprocess；websocket / TCP 模式见 spec 的 transport 章节，但不在 LSP spec 主线，外部约定）。
- **3.18 RFC** 的具体落地时间不明；spec 主页只有 `@since 3.18.0 @proposed` 注解，spec URL 仍是 3.17。MVP 不必为 3.18 让路。
- pyright 通过 npm 包发布，**Python 装的 `pyright` 是 wrapper**（会去 spawn `node ./node_modules/.../pyright-langserver.js`），如果用户本机没装 Node，pyright wrapper 会自己下载一个 Node — 这是 BareAgent 启动检测时可能踩坑的地方。
