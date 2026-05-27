# MCP Protocol Spec — Research Notes

> 日期：2026-05-27　目的：为 BareAgent MCP 客户端实现提供权威协议参考

## 协议版本与稳定性

- **最新稳定版本号**：`"2025-06-18"`（字符串形式的日期版本号，作为 `protocolVersion` 字段值）
- 上一版本：`"2024-11-05"`（许多生产服务器仍只声明这个；客户端协商时需要回退兼容）
- 规范状态：稳定（已发布于 modelcontextprotocol.io/specification/2025-06-18），不是 preview
- Source of truth：[TypeScript schema](https://github.com/modelcontextprotocol/specification/blob/main/schema/2025-06-18/schema.ts) + 自动生成的 [JSON Schema](https://github.com/modelcontextprotocol/specification/blob/main/schema/2025-06-18/schema.json)
- 传输层：JSON-RPC 2.0 over stdio 或 HTTP（HTTP 时必须带 `MCP-Protocol-Version` header）

## 初始化握手

握手是 **三步**：`initialize` request → `initialize` response → `notifications/initialized`（客户端单向通知）。

### Request schema (client → server)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-06-18",
    "capabilities": {
      "roots": { "listChanged": true },
      "sampling": {},
      "elicitation": {}
    },
    "clientInfo": {
      "name": "ExampleClient",
      "title": "Example Client Display Name",
      "version": "1.0.0"
    }
  }
}
```

关键字段：
- `protocolVersion`：客户端期望使用的版本（**SHOULD** 是 client 支持的最新版本）
- `capabilities`：客户端能力对象，见下文
- `clientInfo.name` / `clientInfo.version`：必填；`title` 是 2025-06-18 新增的可读显示名（可选）

### Response schema (server → client)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {
      "logging": {},
      "prompts": { "listChanged": true },
      "resources": { "subscribe": true, "listChanged": true },
      "tools": { "listChanged": true }
    },
    "serverInfo": {
      "name": "ExampleServer",
      "title": "Example Server Display Name",
      "version": "1.0.0"
    },
    "instructions": "Optional instructions for the client"
  }
}
```

关键字段：
- `protocolVersion`：如果服务器支持客户端请求的版本，必须返回相同版本；否则返回服务器支持的最新版本，由客户端决定是否继续
- `instructions`：可选的自然语言使用说明，**客户端可以注入到系统提示词中**

### `initialized` notification

握手收尾，客户端单向通知（**无 id**）：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}
```

规则：
- 客户端在收到 initialize response 前 **不应** 发其它请求（ping 除外）
- 服务器在收到 initialized 通知前 **不应** 发其它请求（ping、logging 除外）

### 版本协商规则

- 客户端发版本 → 服务器若支持则返回相同版本，否则返回自己支持的最新版本
- 客户端若不支持服务器返回的版本，**SHOULD** 断开连接
- HTTP 传输：握手完成后，客户端所有后续请求 **MUST** 带 `MCP-Protocol-Version: <version>` HTTP header

## 能力声明（capabilities）

Capability 对象的两层结构：顶层 key 表示功能类别，value 是对象（可空 `{}`），对象内可声明 sub-capability。**空对象 `{}` 表示支持该功能但无子能力**；省略该 key 等于不支持。

### Client capabilities

| Key | 含义 | Sub-fields |
|---|---|---|
| `roots` | 暴露文件系统/URI 根目录 | `listChanged: bool` |
| `sampling` | 接受服务器发起的 LLM 采样请求 | （空对象） |
| `elicitation` | 接受服务器发起的用户输入请求（2025-06-18 新增） | （空对象） |
| `experimental` | 非标准实验功能 | 自定义 |

### Server capabilities

| Key | 含义 | Sub-fields |
|---|---|---|
| `tools` | 提供可调用工具 | `listChanged: bool` |
| `resources` | 提供可读资源 | `subscribe: bool`, `listChanged: bool` |
| `prompts` | 提供 prompt 模板 | `listChanged: bool` |
| `logging` | 发结构化日志 | （空对象） |
| `completions` | 参数自动补全 | （空对象） |
| `experimental` | 非标准 | 自定义 |

### Sub-capability 语义

- `listChanged`：服务器在工具/资源/Prompt 列表变化时会发 `notifications/<feature>/list_changed`，客户端应订阅并重新 list
- `subscribe`：仅 resources 有；允许客户端 `resources/subscribe <uri>`，服务器在该资源变更时发 `notifications/resources/updated`

**协商原则**：双方在 operation 阶段只能使用 **协商通过的** 能力，未声明的功能即使代码支持也不能使用。

## Tools

### `tools/list` 响应结构

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "get_weather",
        "title": "Weather Information Provider",
        "description": "Get current weather information for a location",
        "inputSchema": {
          "type": "object",
          "properties": {
            "location": { "type": "string", "description": "City name or zip code" }
          },
          "required": ["location"]
        }
      }
    ],
    "nextCursor": "next-page-cursor"
  }
}
```

支持 **分页**（请求带 `cursor`，响应带 `nextCursor`）。

### Tool schema 字段

- `name`：唯一标识（字符串）
- `title`：可选可读显示名（2025-06-18 新增）
- `description`：功能描述
- `inputSchema`：**标准 JSON Schema**，顶层 `type` 必须是 `"object"`（即所有参数包成对象）
- `outputSchema`：可选 JSON Schema，用于校验 structured output
- `annotations`：可选行为元数据（**客户端 MUST 视为不可信**，除非服务器本身可信）

> **重要**：`inputSchema` 是合法 JSON Schema 子集（Draft 7 兼容），可以直接喂给 LLM provider 的 tool calling schema（Anthropic / OpenAI）几乎零转换。

### `tools/call` 请求 / 响应

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "get_weather",
    "arguments": { "location": "New York" }
  }
}
```

Response（成功）：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      { "type": "text", "text": "Current weather in New York: ..." }
    ],
    "isError": false
  }
}
```

`result` 字段：
- `content`：内容块数组（unstructured）
- `structuredContent`：可选，JSON 对象（对应 `outputSchema`；向后兼容时 **应同时** 在 content 里放序列化 JSON 的 TextContent）
- `isError`：布尔值，工具执行级错误标志（**不同于 JSON-RPC error**）

### Content blocks

5 种类型，由 `type` 字段区分：

1. **TextContent**
   ```json
   { "type": "text", "text": "..." }
   ```

2. **ImageContent**
   ```json
   { "type": "image", "data": "<base64>", "mimeType": "image/png" }
   ```

3. **AudioContent**
   ```json
   { "type": "audio", "data": "<base64>", "mimeType": "audio/wav" }
   ```

4. **ResourceLink**（只是 URI 指针，客户端按需 fetch）
   ```json
   {
     "type": "resource_link",
     "uri": "file:///project/src/main.rs",
     "name": "main.rs",
     "description": "...",
     "mimeType": "text/x-rust"
   }
   ```

5. **EmbeddedResource**（内联资源内容）
   ```json
   {
     "type": "resource",
     "resource": {
       "uri": "file:///project/src/main.rs",
       "mimeType": "text/x-rust",
       "text": "fn main() { ... }"
     }
   }
   ```
   或二进制：`"blob": "<base64>"` 代替 `text`。

所有 content block 支持可选 `annotations`（audience / priority / lastModified）。

## Resources

### `resources/list` 响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "resources": [
      {
        "uri": "file:///project/src/main.rs",
        "name": "main.rs",
        "title": "Rust Software Application Main File",
        "description": "Primary application entry point",
        "mimeType": "text/x-rust"
      }
    ],
    "nextCursor": "next-page-cursor"
  }
}
```

支持分页。`resources/templates/list` 是单独的方法（用于 URI templates 参数化资源）。

### Resource 描述符字段

- `uri`：唯一标识，必须符合 RFC3986
- `name`：必填
- `title`：可选显示名
- `description`：可选
- `mimeType`：可选
- `size`：可选字节数
- `annotations`：可选（audience / priority / lastModified）

常见 URI scheme：`file://`、`https://`、`git://`，也允许自定义 scheme。

### `resources/read` 请求 / 响应

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "resources/read",
  "params": { "uri": "file:///project/src/main.rs" }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "contents": [
      {
        "uri": "file:///project/src/main.rs",
        "mimeType": "text/x-rust",
        "text": "fn main() { ... }"
      }
    ]
  }
}
```

`contents` 是数组（单个 URI 可能展开成多个 entries，例如目录读取）。每个 entry：
- 必有 `uri` + `mimeType`
- **二选一**：`text`（字符串文本）或 `blob`（base64 二进制）

### 订阅

- `resources/subscribe { uri }` → 服务器在该资源变更时发 `notifications/resources/updated { uri }`
- 需要服务器声明 `resources.subscribe: true`

## Prompts

### `prompts/list` 响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "prompts": [
      {
        "name": "code_review",
        "title": "Request Code Review",
        "description": "Asks the LLM to analyze code quality and suggest improvements",
        "arguments": [
          { "name": "code", "description": "The code to review", "required": true }
        ]
      }
    ],
    "nextCursor": "next-page-cursor"
  }
}
```

### Prompt schema（arguments 模型）

- `name`：唯一标识
- `title`：可选显示名
- `description`：可选
- `arguments`：可选数组，每项 `{ name, description?, required? }`

**注意**：argument 定义本身 **不是** JSON Schema，只是名字列表 + required 标志。**变量没有类型约束**，统一按字符串处理（通过 completion API 可做自动补全提示）。

### `prompts/get` 请求 / 响应

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "prompts/get",
  "params": {
    "name": "code_review",
    "arguments": { "code": "def hello():\n    print('world')" }
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "description": "Code review prompt",
    "messages": [
      {
        "role": "user",
        "content": { "type": "text", "text": "Please review this Python code:\n..." }
      }
    ]
  }
}
```

`messages` 数组每条：
- `role`：`"user"` 或 `"assistant"`
- `content`：单个内容块（**注意是单对象而非数组**，与 tools/call 的 content array 不同），类型同 tools 的 content block：text / image / audio / resource

服务器负责完成变量替换并返回完整消息列表，客户端把它当 chat history 注入对话。

## 错误约定

### 双层错误模型

MCP 把错误分两层：

1. **JSON-RPC 协议级错误** — 写在响应顶层的 `error` 字段，沿用 JSON-RPC 2.0 标准码（`-32700`/`-32600`/`-32601`/`-32602`/`-32603`）。MCP 仅扩展了一个：
   - `-32002` Resource not found（带 `data: { uri }`）
2. **工具执行级错误** — 在 `tools/call` 的成功响应里通过 `isError: true` + content 中的描述文本表达。`result` 字段照常存在，**JSON-RPC 层是成功的**。

### 何时用哪一层（重要）

- 工具未注册、参数 schema 不符 → JSON-RPC error（-32602）
- 服务器内部异常 → JSON-RPC error（-32603）
- 工具被正确调用，但业务失败（API 限流、远端 404、计算异常）→ `isError: true` 在 result 里返回。理由：让 LLM 能看到失败原因并自行重试或换策略，而不是中断协议层

示例（执行级错误）：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      { "type": "text", "text": "Failed to fetch weather data: API rate limit exceeded" }
    ],
    "isError": true
  }
}
```

## 与 BareAgent 实现的关键启示

1. **inputSchema 几乎零转换**：MCP 工具的 `inputSchema` 是标准 JSON Schema，可以直接作为 Anthropic `tools[*].input_schema` 或 OpenAI `tools[*].function.parameters` 注入，**仅需把 MCP 工具名加上 server 前缀**（如 `mcp__<server>__<tool>`）避免命名冲突。BareAgent 现有的 `core/schema.py` 风格可无缝接收。

2. **isError 必须翻译成 BareAgent 的工具结果语义**：`tools/call` 返回 `isError: true` 时，BareAgent handler 应把 content 里的文本拼成普通工具错误信息，**不能** 把它升级成 exception 中断 agent loop（否则模型失去重试机会）。这与 `core/loop.py` 中现有失败处理一致。

3. **content array 需要拍扁**：MCP 一次工具调用可能返回多个 content block（text + image + resource_link），而 Anthropic/OpenAI 的 tool_result 通常期待单字符串或受限的多模态块。最稳的策略：把所有 TextContent 串联，把 ImageContent 转为 provider 原生格式，EmbeddedResource 序列化为 `<resource uri=...>\n<text>\n</resource>` 文本块。

4. **能力协商必须 fail-closed**：BareAgent 在握手后应只暴露服务器声明的功能（声明 `tools` 才调 `tools/list`）。未声明 `listChanged` 时不要订阅相应通知。这跟 `permission/guard` 的 fail-closed 风格相符。

5. **resources 的 mimeType + text/blob 二分**：解码时按 `text` 或 `blob` 字段二选一；`blob` 必须 base64 解码后才能交给上层。对于 LLM 注入，建议把 text 资源直接以文本拼入，blob 资源仅在 provider 支持 image/audio 时才透传，否则降级为 `<binary resource uri=...>` 占位。

6. **`instructions` 字段是廉价上下文**：服务器返回的 `serverInfo.instructions` 应该被 BareAgent 拼到 system prompt 末尾（类似 skills 的加载效果），让模型了解该 MCP server 的用途。

7. **协议版本回退**：保险做法是客户端先发 `"2025-06-18"`，若服务器返回 `"2024-11-05"` 也接受继续工作（多数旧版字段是子集兼容），仅在收到无法识别的版本时才断开。

## 来源

- 规范主页：<https://modelcontextprotocol.io/specification/2025-06-18>
- Lifecycle / 初始化：<https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle>
- Base Protocol（JSON-RPC、错误码、_meta）：<https://modelcontextprotocol.io/specification/2025-06-18/basic>
- Server / Tools：<https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- Server / Resources：<https://modelcontextprotocol.io/specification/2025-06-18/server/resources>
- Server / Prompts：<https://modelcontextprotocol.io/specification/2025-06-18/server/prompts>
- TypeScript schema (source of truth)：<https://github.com/modelcontextprotocol/specification/blob/main/schema/2025-06-18/schema.ts>
- JSON Schema（自动生成）：<https://github.com/modelcontextprotocol/specification/blob/main/schema/2025-06-18/schema.json>
