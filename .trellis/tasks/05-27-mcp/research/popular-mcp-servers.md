# Popular MCP Servers — Sample Survey

调研日期：2026-05-27。样本来自 `github.com/modelcontextprotocol/servers`（官方 reference servers）+ 第三方 `upstash/context7` + `mcp-servers-archived/postgres`。

## 抽样选取

| Server | 实现语言 | 选取理由 |
| --- | --- | --- |
| **filesystem** | TypeScript (SDK + Zod) | 官方旗舰；schema 多样（含数组、可选、defaults） |
| **fetch** | Python (SDK + Pydantic) | 验证 Python 侧 schema 形态（`anyOf` for Optional） |
| **git** | Python (SDK + Pydantic) | 工具数量多、Pydantic 自动 `model_json_schema()` 输出 |
| **everything** | TypeScript | 协议演示库，覆盖 image / resource_link / annotations / prompts |
| **memory** | TypeScript | 嵌套对象数组（Zod → `$ref` + `$defs`） |
| **postgres**（archived） | TypeScript | 含 Resources（`postgres://` URI 风格） |
| **context7** | TypeScript (上层 wrapper) | 第三方 server 的真实形态参照 |

---

## 各 server 详解

### 1. filesystem（@modelcontextprotocol/server-filesystem）

- **维护方**：官方 / Anthropic。**传输**：stdio。**安装**：`npx -y @modelcontextprotocol/server-filesystem <path>`。
- **注册方式**：`server.registerTool(name, {inputSchema: <zodShape>, ...}, handler)`，SDK 内部自动用 `zod-to-json-schema` 转换为 JSON Schema 暴露给 client。
- **典型 tools**：
  - `read_text_file` → 真实 schema 片段（client 拿到的 JSON）：
    ```json
    {"type":"object","properties":{
      "path":{"type":"string"},
      "tail":{"type":"number","description":"..."},
      "head":{"type":"number","description":"..."}
    },"required":["path"]}
    ```
  - `edit_file` —— 数组+嵌套对象+默认值：
    ```json
    {"type":"object","properties":{
      "path":{"type":"string"},
      "edits":{"type":"array","items":{"type":"object",
        "properties":{"oldText":{"type":"string"},"newText":{"type":"string"}},
        "required":["oldText","newText"]}},
      "dryRun":{"type":"boolean","default":false}
    },"required":["path","edits"]}
    ```
  - `list_allowed_directories` → `inputSchema: {}`（空对象，client 必须接受零参数工具）。
- **结果内容**：主要 `TextContent`；`read_media_file` 返回 `ImageContent` / `AudioContent` / `blob`（含 `mimeType` + base64 `data`）。
- **annotations**：每个工具带 `readOnlyHint / idempotentHint / destructiveHint`。
- **Resources/Prompts**：无（filesystem 不暴露 resources）。

### 2. fetch（mcp-server-fetch）

- **维护方**：官方。**传输**：stdio。**安装**：`uvx mcp-server-fetch`。
- **注册方式**：Python SDK `Tool(name=..., inputSchema=Fetch.model_json_schema())` —— 直接用 Pydantic 的 JSON Schema 输出。
- **唯一工具** `fetch`，真实 schema 形如：
  ```json
  {"type":"object","title":"Fetch","properties":{
    "url":{"type":"string","format":"uri","minLength":1,"description":"URL to fetch"},
    "max_length":{"type":"integer","exclusiveMinimum":0,"exclusiveMaximum":1000000,"default":5000},
    "start_index":{"type":"integer","minimum":0,"default":0},
    "raw":{"type":"boolean","default":false}
  },"required":["url"]}
  ```
  → 出现了 `format`、`exclusiveMinimum/Maximum`、`minLength` 等 OpenAPI 风味关键字。
- **Prompts**：暴露同名 `fetch` prompt，含 `arguments: [{name:"url", required:true}]`。
- **结果内容**：仅 `TextContent`（markdown 化的网页）。

### 3. git（mcp-server-git）

- **维护方**：官方。**传输**：stdio。**安装**：`uvx mcp-server-git`。
- **13 个工具**，每个对应一个 Pydantic 类；schema 经 `.model_json_schema()` 输出，**关键观察**：
  - `Optional[str] = Field(None, ...)` 会被序列化成 `"anyOf":[{"type":"string"},{"type":"null"}]` —— 这是 Pydantic v2 的默认行为，是 **大多数 Python MCP server 客户端必须能处理的形态**。
  - 每个属性都有自动生成的 `"title"`（如 `"Repo Path"`），多余但无害。
  - 顶层附带 `"title": "GitLog"`、`"type": "object"`、`"required":[...]`。
- 例：`git_log` 实际 schema：
  ```json
  {"type":"object","title":"GitLog","properties":{
    "repo_path":{"title":"Repo Path","type":"string"},
    "max_count":{"default":10,"title":"Max Count","type":"integer"},
    "start_timestamp":{"anyOf":[{"type":"string"},{"type":"null"}],
      "default":null,"description":"Start ts","title":"Start Timestamp"}
  },"required":["repo_path"]}
  ```
- **结果**：纯 `TextContent`。无 resources / prompts。

### 4. everything（demo server）

- **维护方**：官方协议演示。**传输**：stdio / HTTP（演示多 transport）。
- **覆盖几乎所有协议特性**：
  - tools 含 enum：`get-annotated-message` 用 `z.enum(["error","success","debug"])` → JSON `"enum":["error","success","debug"]`。
  - 空 inputSchema：`get-tiny-image`, `get-env`, `trigger-elicitation-request` 等都 `inputSchema: {}`。
  - 含 `min/max/default`：`get-resource-links` 的 `count: z.number().min(1).max(10).default(3)`。
- **Resources**：URI 风格 `demo://resource/dynamic/text/{id}`、`demo://resource/dynamic/blob/{id}`，自定义 scheme + 路径模板。`mimeType`: `text/plain` 或 `application/octet-stream`。
- **Prompts**：含 `argsSchema: {city: z.string(), state: z.string().optional()}` —— prompt 的 `arguments` 字段实质就是 `{name, description, required}` 三元组列表，**不是完整 JSON Schema**。
- **结果内容**：示范了 `text` / `image` / `audio` / `resource_link` / `embedded resource` 五种 content block 形态，并带 `annotations: {priority, audience}`。

### 5. memory（@modelcontextprotocol/server-memory）

- 注册时用 `z.array(z.object({...}))`，SDK 转换后会产生 `$ref` + `$defs`（嵌套对象常见做法）：
  ```json
  {"type":"object","properties":{
    "entities":{"type":"array","items":{"$ref":"#/$defs/Entity"}}
  },"$defs":{"Entity":{"type":"object","properties":{
    "name":{"type":"string"},"entityType":{"type":"string"},
    "observations":{"type":"array","items":{"type":"string"}}}}}}
  ```
- **必须** 支持 `$ref` 解引用（至少能透传给 LLM）。

### 6. postgres（archived reference）

- **传输**：stdio。**工具** `query`：`{type:"object",properties:{sql:{type:"string"}},required:["sql"]}` —— 极简。
- **Resources** URI 风格 `postgres://[host]/[tablename]/schema`，`mimeType: application/json`。展示了 "URI 自带 host/path/段位" 的命名约定。

### 7. context7（第三方）

- TypeScript MCP wrapper（npx `@upstash/context7-mcp`）。
- 两个工具，schema 仅含 `string` 属性 + 详细 description（重 prompt engineering、轻 schema）：
  - `resolve-library-id`: `{query, libraryName}`，皆 required string。
  - `query-docs`: `{libraryId, query}`。
- 无 resources、无 prompts。结果纯文本。

---

## 共性观察

### inputSchema 是否真的全部是标准 JSON Schema 子集？
**几乎是。** 所有 7 个样本的 inputSchema 都符合 JSON Schema Draft 2020-12 子集，可直接喂给绝大多数 LLM 的 tool-use 接口。两类生成器有显著差异：

| 来源 | 易出现的关键字 |
| --- | --- |
| Zod (TS) → zod-to-json-schema | `$defs` + `$ref`（嵌套对象时）；`enum`；`default`；规整无多余字段 |
| Pydantic (Py) → model_json_schema | `anyOf:[{type},{type:"null"}]`（Optional）；自动 `title`；`format` (uri/email)；`exclusiveMinimum` 等 |

### 高频关键字（**必须**支持）
`type` (object/string/integer/number/boolean/array/null), `properties`, `required`, `items`, `description`, `default`, `enum`, `title`。

### 中频（**应该**支持）
`$ref` + `$defs`（Zod 嵌套和 Pydantic 嵌套都会产生），`anyOf`（Pydantic Optional 必出），`minimum/maximum/exclusiveMinimum/exclusiveMaximum`, `minLength/maxLength`, `format` (uri 居多)。

### 低频 / 罕见（**可忽略**）
- `oneOf` —— 7 个样本中 **零出现**。
- `allOf` —— 零出现。
- `additionalProperties` —— 偶尔在 Pydantic 输出，但语义上 client 通常不需要校验，可忽略。
- `patternProperties`、`dependencies`、`if/then/else` —— 零出现。
- `not` —— 零出现。

### Resource URI 风格汇总

| Server | URI 模板 | mimeType |
| --- | --- | --- |
| everything | `demo://resource/dynamic/text/{id}` / `.../blob/{id}` | `text/plain` / `application/octet-stream` |
| postgres | `postgres://host/{tablename}/schema` | `application/json` |
| filesystem | (无 resources) | — |
| memory | (无 resources) | — |

观察：scheme 都是自定义的（`demo://`, `postgres://`, `file://`, `git://`），**没有**任何 server 试图用 `https://`。client 端 URI 解析不能假设 scheme 集合，只能按通用 `<scheme>://<opaque>` 处理。

### Content block 种类

| 种类 | 出现 server |
| --- | --- |
| `text` | 全部 7 个（必须） |
| `image` | filesystem, everything |
| `audio` | filesystem |
| `resource_link` | everything |
| `embedded_resource` | everything |
| `blob`（非标准 fallback） | filesystem 的 read_media_file 用了一次 |

→ MVP 实现：`text` 必撑、`image`/`audio` 做透传（仅 base64 + mimeType）、`resource_link` 当成富 text 渲染、`embedded_resource` 先 stub 提示。

---

## 对 BareAgent 实现的启示

### Schema 转换器必须覆盖的字段
1. `type` 全部 7 种 + `null`。
2. `properties` / `required` / `items` 递归遍历。
3. `description` / `default` / `enum` 直通透传。
4. `anyOf`（Pydantic Optional）—— **不能丢**，否则 git 这类 Python server 的所有可选参数会丢类型。
5. `$ref` + `$defs`：最低限度做 **inline 展开**（resolve `#/$defs/X` 替换为 `$defs.X`），因为部分 LLM 提供商（特别是 OpenAI 老接口）不识别 `$ref`。Anthropic 接口允许 `$ref` 透传，但保险起见展开更稳。
6. `title` 字段：Pydantic 大量产生但语义无用 —— **静默丢弃** 即可（不要透传给 LLM，省 token）。

### 可暂时忽略 / 兜底处理
- `oneOf`、`allOf`、`not`、`if/then/else`、`patternProperties`：实测样本 **零出现**。遇到时不必抛错，直接透传 schema 让 LLM 自己解释；不主动校验。
- `format`（uri / email / date-time）：透传字符串字面量，不做运行时校验。
- `additionalProperties: false` 等：透传，client 不强制。

### 兜底策略
对于无法识别的关键字：**透传给 LLM、不抛错**。已知 LLM 对 JSON Schema 的容忍度高（多数会忽略未知字段）。在 BareAgent 内部 schema 模型中预留一个 `extra: dict` 字段承接未识别的关键字。

### 实现建议
- 单一转换函数 `mcp_schema_to_tool_param(schema: dict) -> dict`，做 4 件事：
  1. 解析顶层 `type` / `properties` / `required`。
  2. 递归处理 `properties.*` 与 `items`。
  3. inline `$ref` -> `$defs[*]`（递归一次，防循环）。
  4. 丢弃 `title`、保留 `description / default / enum / anyOf`。
- 不做 schema 校验（参数校验交给 server 本身，client 只负责"传递"）。

---

## 来源

- 官方仓库（已 clone 到 `/tmp/mcp-servers`）：
  - https://github.com/modelcontextprotocol/servers
  - `src/filesystem/index.ts`, `src/fetch/src/mcp_server_fetch/server.py`, `src/git/src/mcp_server_git/server.py`, `src/memory/index.ts`, `src/everything/tools/*.ts`, `src/everything/resources/templates.ts`, `src/everything/prompts/args.ts`
- 第三方：
  - https://github.com/upstash/context7（packages/mcp/src/index.ts，Web 抓取）
  - https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres（Web 抓取）
- Pydantic 行为验证：本地 `.venv/Scripts/python` 渲染 `Fetch.model_json_schema()` 与嵌套模型，确认 `anyOf` 与 `$defs/$ref` 输出形态。
