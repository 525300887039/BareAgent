# PR5: MCP 多模态结果回传 + provider 适配

> 父任务：`.trellis/tasks/05-27-mcp`（完整 MCP 客户端规划）
> 前置 PR：PR1（transport+protocol）✅、PR2（client+manager+registry+tools 注入）✅、PR3（resources+prompts）✅、PR4（permission+isolation+REPL）✅

## Goal

打通 **MCP server 返回 image 内容 → LLM 下一轮能看到** 的端到端通路。当前实现的占位 `[image omitted: PR5]` 在 PR5 替换为真实多模态回传：

- **core loop**：`_tool_result(output)` 接受 `list[dict]` 多内容块（保留 `str` 向后兼容）
- **registry**：`_flatten_content` 现有逻辑改造为 `_to_content_blocks(content) -> list[dict]`，image 块从 MCP 格式（`{type: "image", data, mimeType}`）规范化为 BareAgent 内部格式（`{type: "image", source: {type: "base64", media_type, data}}`，与 Anthropic 格式对齐）
- **Anthropic provider**：`_convert_tool_result_content` 新增 image 块透传（直接用 BareAgent 内部格式，因为它跟 Anthropic 原生格式对齐）
- **OpenAI provider**：tool message content 仅放 text；image 块"提升"为紧跟其后的 user message `[{type: text}, {type: image_url, image_url: {url: "data:<mime>;base64,<data>"}}]`（OpenAI tool role 不接受 image）

本 PR **不做**：payload 截断 / 进程崩溃恢复 / atexit / `/mcp reload` 增强 / E2E 真机冒烟（PR6）。**Audio / embedded_resource / resource_link** 仍降级为带 URI 的占位文本（提供商支持成熟前不做原生）。

## Requirements

### `src/core/loop.py::_tool_result` 改造
- 签名扩展为 `output: str | list[dict[str, Any]]`：
  ```python
  def _tool_result(
      tool_use_id: str,
      output: str | list[dict[str, Any]],
      *,
      is_error: bool = False,
  ) -> dict[str, Any]:
      if isinstance(output, list):
          content: Any = output
      else:
          content = stringify(output)
      result: dict[str, Any] = {
          "type": "tool_result",
          "tool_use_id": tool_use_id,
          "content": content,
      }
      ...
  ```
- 所有现有 caller 不需要改动（继续传 string）
- 仅 MCP handler 通过新路径传 list[dict]

### `src/mcp/registry.py` 改造
- 抽出新公共函数 `_to_content_blocks(mcp_content: list[dict]) -> list[dict]`：
  - 输入：MCP `content` 数组（每项 `{type, ...}`）
  - 输出：BareAgent 内部 content block 数组：
    - MCP `{type: "text", text: T}` → `{type: "text", text: T}`（原样）
    - MCP `{type: "image", data: B64, mimeType: M}` → `{type: "image", source: {type: "base64", media_type: M, data: B64}}`
    - MCP `{type: "audio", ...}` → `{type: "text", text: "[Audio omitted: not supported by current providers]"}`
    - MCP `{type: "embedded_resource", resource: {uri, mimeType, ...}}` → `{type: "text", text: f"[Resource: {uri} ({mimeType})]"}`
    - MCP `{type: "resource_link", uri, ...}` → `{type: "text", text: f"[Resource link: {uri}]"}`
    - 未知 type → `{type: "text", text: f"[Unknown content block: {type}]"}`
- 公共 `_flatten_content(content) -> str`（PR2/PR3 已有，仅 text 串联）保留——给 prompts 注入 transcript 时用（user/assistant 消息一律 text）
- handler 改造：
  - `mcp__<server>__<tool>` (tools/call) handler：成功路径返回 `_to_content_blocks(result["content"])` 这个 list；不再返回 string。错误路径（unhealthy / MCPCallError / isError:true）仍返回 string（`Error: ...`），让 loop 走 string path
  - `mcp__<server>__resource_read` handler：同款——成功返回 list[dict]，错误返回 string
  - `mcp__<server>__resource_list` handler：本质是元信息列表 → 维持 string 返回（无 image）
- handler 返回类型现在是 `str | list[dict]` —— loop 中调 handler 后传给 `_tool_result(call.id, output)`，新签名兼容两种

### `src/provider/anthropic.py` 改造
- `_convert_tool_result_content` 增加 image 块识别：
  ```python
  if isinstance(content, list):
      blocks = []
      for item in content:
          if isinstance(item, dict) and item.get("type") == "text":
              blocks.append({"type": "text", "text": item.get("text", "")})
          elif isinstance(item, dict) and item.get("type") == "image":
              # BareAgent 内部格式已与 Anthropic 对齐
              source = item.get("source", {})
              if isinstance(source, dict) and source.get("type") == "base64":
                  blocks.append({
                      "type": "image",
                      "source": {
                          "type": "base64",
                          "media_type": source.get("media_type", "image/png"),
                          "data": source.get("data", ""),
                      },
                  })
              else:
                  blocks.append({"type": "text", "text": self._stringify_content(item)})
          else:
              blocks.append({"type": "text", "text": self._stringify_content(item)})
      return blocks
  ```

### `src/provider/openai.py` 改造
- `_convert_user_content`（处理 tool_result 段的方法，~line 395）需要拆分：
  - tool_result content 是 list[dict]：分离 text 块和 image 块
    - text 块串联 → 放入 `{role: "tool", tool_call_id, content: <串联文本>}`（仍是 string）
    - 如果有 image 块（≥ 1）→ 追加紧跟其后的 `{role: "user", content: [{type: "text", text: "[Image(s) from tool result]"}, {type: "image_url", image_url: {url: "data:<mime>;base64,<data>"}}, ...]}` user message，承载 image
  - tool_result content 是 string：保留现有路径不变
- **理由**：OpenAI tool role message content 不接受 image_url 类型；标准做法是单独 user message 承载 image。LLM 拿到 tool result text 后看到下一条 user message 里的 image，能正确关联（这是 OpenAI Vision API 推荐 pattern）

### `_serialize_tool_calls` / `_tool_result` 调用点不变
- `src/core/loop.py` 现有所有 caller（permission denied / handler error / 正常返回）维持 string output
- 仅当 handler 返回值是 list[dict]（即 MCP handler 多模态路径）时走新分支

## Acceptance Criteria

- [ ] `_tool_result(id, "text")` 现有行为不变（向后兼容）
- [ ] `_tool_result(id, [{type:"text", text:"hi"}, {type:"image", source:{...}}])` 返回 `{type:"tool_result", content: [...]}` 透传 list
- [ ] MCP handler 返回 list[dict] → loop append message → Anthropic provider 序列化时 image 块正确转 `{type:"image", source:{type:"base64", media_type, data}}`
- [ ] 同样的 list → OpenAI provider 序列化时 tool message 只含 text，紧跟一条 user message 含 image_url（data URL 格式）
- [ ] MCP audio 块 → 降级为 `[Audio omitted: not supported by current providers]` 占位文本
- [ ] MCP embedded_resource → 占位带 URI + mimeType
- [ ] MCP resource_link → 占位带 URI
- [ ] 未知 type → 占位带 type 名（不抛异常）
- [ ] PR1-4 共 412 个测试全绿（不退化）
- [ ] 至少 12 个新 pytest case 覆盖：_to_content_blocks 6 种 type、_tool_result 双签名、Anthropic image 透传、OpenAI image 提升、tool result string 路径不变（回归）

## Definition of Done

- 改动落在 `src/core/loop.py` + `src/mcp/registry.py` + `src/provider/anthropic.py` + `src/provider/openai.py`
- 新增测试 `tests/test_mcp_multimodal.py`（覆盖 _to_content_blocks 单元 + provider serialization 端到端）
- 扩展 `tests/test_anthropic.py` + `tests/test_openai.py`（如已存在）覆盖 image tool result 序列化
- `ruff check src tests` + `ruff format src tests` 全绿
- `pytest` 全集合 green
- **禁动文件**：`src/mcp/transport/*` / `src/mcp/protocol.py` / `src/mcp/_sse.py` / `src/mcp/config.py` / `src/mcp/errors.py` / `src/mcp/client.py` / `src/mcp/manager.py` / `src/permission/*` / `src/planning/agent_types.py` / `src/main.py`

## Technical Approach

### 数据流（核心）
```
MCP server 返回 tools/call result:
  {content: [{type:"text", text:"..."}, {type:"image", data:"<b64>", mimeType:"image/png"}], isError:false}
                                  ↓
src/mcp/registry.py::_to_content_blocks 规范化:
  [{type:"text", text:"..."}, {type:"image", source:{type:"base64", media_type:"image/png", data:"<b64>"}}]
                                  ↓
src/mcp/registry.py::handler 返回这个 list[dict]
                                  ↓
src/core/loop.py::_tool_result(call.id, output=list) → 包成 {type:"tool_result", content: <list>}
                                  ↓
messages.append({role: "user", content: [tool_result_block]})
                                  ↓
provider.anthropic.py 序列化:
  消息直接发给 Anthropic API (image block 已是原生格式)
                                  ↓
provider.openai.py 序列化:
  tool message content = "..."（text only），紧跟 user message [{type:text}, {type:image_url}]
```

### 内部 image block 格式选择
- BareAgent 内部 image block 采用 **Anthropic 原生格式** `{type:"image", source:{type:"base64", media_type, data}}`
- 理由：Anthropic 格式是受支持类型最少的，转 OpenAI / 未来 Gemini 都是单向映射；如果用通用格式（如 `{type:"image", mime, data}`）反而需要双向转换
- OpenAI 转换在 provider 内部按规则映射：`source.data + source.media_type` → `data:<mime>;base64,<data>`

### `_to_content_blocks` 容错策略
- 缺字段（如 image 块没 `data` / `mimeType`）→ 降级为 `[Image omitted: missing required field <X>]` 文本，**不抛异常**
- mimeType 不在白名单（仅 image/png, image/jpeg, image/gif, image/webp）→ 降级为占位文本（Anthropic API 只接受这 4 种）
- base64 data 空字符串 → 占位 `[Image omitted: empty data]`

### OpenAI image 提升的具体语义
- tool message:
  ```python
  {"role": "tool", "tool_call_id": "<id>", "content": "<text 块串联>" or "[No text content; image attached separately]"}
  ```
- 紧跟一条 user message（**仅当 image 块 ≥ 1**）：
  ```python
  {"role": "user", "content": [
      {"type": "text", "text": f"[Tool '<tool_name>' returned {N} image(s)]"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<b64>"}},
      ...
  ]}
  ```
- 多张 image 都放进同一条 user message 的 content 数组
- 非 vision OpenAI model 收到 image_url 会 API 报错——这是 model 选择问题，BareAgent 不做能力检测（用户自己切模型）

### `_tool_result` 现有 caller 行为
- 现有所有 caller 传 string（permission denied / handler exception / 大多数 handler 成功）→ 走 `stringify(output)` 现有路径
- 仅 MCP handler 多模态成功路径传 list[dict] → 走新路径

## Decision (ADR-lite)

**Context**：父 PRD 锁定多模态 image 端到端通路，但两个 provider 处理 image 的能力不对等——Anthropic 原生支持 tool_result 含 image block，OpenAI tool role 不接受 image。需要 BareAgent 内部统一抽象。

**Decision**：
- 内部 image block 用 **Anthropic 原生格式**作为 BareAgent 标准
- OpenAI 适配用 **image 提升 + 紧跟 user message** pattern（OpenAI Vision API 推荐做法）
- Audio / embedded_resource / resource_link 仍降级文本（provider 支持不成熟）
- 不做 payload 截断 / 能力检测 / model 切换提示——留 PR6 / v2

**Consequences**：
- ✅ Anthropic 路径零成本（格式对齐）
- ✅ OpenAI Vision model（gpt-4o 等）能正确看到 image
- ⚠️ OpenAI 非 vision model + image 工具结果 → API 报错，用户需切 model（v2 加能力检测）
- ⚠️ tool_result 后追加 user message 改变了消息序列，所有依赖严格 user/assistant 交替的 hook 需注意——但 OpenAI API 本身允许连续 user message，不破契约
- ⚠️ Audio 模态延迟到 provider 普遍支持后再做

## Out of Scope (explicit)

- **Audio 原生回传**：provider 支持成熟后再做（v2+）
- **Embedded resource / resource link 内容下载并嵌入**：v1 只暴露 URI 占位（让 LLM 通过 resource_read 主动拉）
- **Payload 截断**（256KB text / 5MB binary）→ PR6
- **MimeType 完整白名单 + Anthropic API 校验**：v1 仅常见 4 种 image mime（png/jpeg/gif/webp），其他降级
- **OpenAI 能力检测**（vision vs non-vision model 分支）→ v2
- **Gemini / DeepSeek-vl 等其他 provider 多模态**：v1 仅 Anthropic + OpenAI
- **图像格式转换**（PNG → JPEG 之类的服务端转码）→ 不做
- **atexit / `/mcp reload` 增强 / 进程崩溃恢复**：PR6
- **完整 E2E 真机冒烟**（mcp-server-fetch 实际拉 image）→ PR6

## Technical Notes

- 父任务 PRD 见 `../05-27-mcp/prd.md`
- 关键研究：
  - `../05-27-mcp/research/mcp-protocol-spec.md` — content array 5 种内容块格式（text/image/audio/embedded_resource/resource_link）
- 现有相关代码：
  - `src/core/loop.py::_tool_result` (line 276)：当前签名 `output: Any` → `stringify(output)`
  - `src/provider/anthropic.py::_convert_tool_result_content` (line 182)：已支持 list content，但仅 text 块
  - `src/provider/openai.py::_convert_user_content` (line ~395)：完全不支持 list content
  - `src/mcp/registry.py::_flatten_content`：PR2/PR3 已有，输出 string；PR5 新增 `_to_content_blocks` 输出 list[dict]
- API 格式参考：
  - Anthropic: <https://docs.anthropic.com/en/api/messages> tool_result content with image source
  - OpenAI: <https://platform.openai.com/docs/guides/vision> chat completions with image_url
- 必须遵循 `.trellis/spec/backend/`：
  - `error-handling.md`：image 块缺字段 → 降级为占位文本，不抛异常；handler 多模态错误路径仍返回 string
  - `quality-guidelines.md`：完整类型注解、ruff 全绿
  - `directory-structure.md`：仅 core/loop + mcp/registry + provider/{anthropic,openai} 改动
