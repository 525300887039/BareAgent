# Design — 多模态 web_fetch 图片 + PDF 原生 document block

两个子目标独立，共用 vision 门控范式。③ 是纯 handler 层小改；④ 跨 handler + provider + config，含一个需实测验证的 API 未知。

## ③ web_fetch 图片

**唯一改动点**：`core/handlers/web_fetch.py:run_web_fetch` + `core/tools.py` 的 web_fetch 绑定。

数据流：
```
run_web_fetch(url, ..., image_enabled)
  └─ urlopen → 读响应头 Content-Type
       ├─ mime ∈ 图片白名单(png/jpeg/gif/webp):
       │     ├─ not image_enabled → _IMAGE_DISABLED_ERROR (复用 file_read 的文案)
       │     ├─ 读原始 bytes 至 _MAX_IMAGE_BYTES+1；超限 → 友好文本提示
       │     └─ 否则 → [ {text: 描述}, {image: base64} ]  (复用 _read_image 的 block 形状)
       ├─ mime 是其它 image/* (svg/bmp/...) → 友好文本提示「下载后处理」
       └─ 否则(html/text/json/...) → 现有文本路径，字节级不变
```

**契约**：
- 返回类型 `str | list[dict]`（`_tool_result` 已支持 list 直通）。
- `image_enabled: bool = True` 默认值保证现有调用字节级不变。
- 图片 mime 白名单 = `file_read._IMAGE_EXT_TO_MIME` 的**值集合**，单一真源（从 file_read 导出复用，不另立表）。判定取 Content-Type mime 主串，忽略 `; charset=` 参数。
- block 形状与编码复用 `file_read._read_image` 的产物（`{type:text}` + `{type:image, source:{base64}}`）——若代码可直接复用该函数则复用，否则抽公共 helper。

**接线**：`core/tools.py:get_handlers` 把 `"web_fetch": run_web_fetch` 改为 `partial(run_web_fetch, image_enabled=image_input_enabled)`。`image_input_enabled` 参数 `get_handlers` 已有（③④ 共用）。web_fetch 不落 workspace，`rebind_workspace_handlers` 不动它（保持父绑定，天然继承门控）。

## ④ PDF 原生 document block

### 能力层 `provider/capabilities.py`
- `KNOWN_PDF_INPUT_PREFIXES = ("claude-3","claude-opus-4","claude-sonnet-4","claude-haiku-4")`——仅 Claude，base64 PDF document 是 Anthropic 特性。
- `supports_pdf_input(model, *, override=None) -> bool`：override 优先；否则前缀命中；**未知 fail-safe deny**（照抄 `supports_image_input` 结构）。

### handler 层 `core/handlers/file_read.py`
- `run_read` 加 `pdf_enabled: bool = True`。
- PDF 分派（在现有 `.pdf` 分支内）：
  - `pages` 非空 → 现有 `_read_pdf`（pypdf），行为字节级不变。
  - 否则 `pdf_enabled` 且 `size ≤ _MAX_PDF_BYTES(20MB)` → 新 `_read_pdf_native`：读 bytes、b64encode、返 `[{text: label}, {document: base64}]`。
  - 否则（无能力 / 超限）→ `_read_pdf`(pypdf) + 一行前缀提示说明回退原因。
- `_MAX_PDF_BYTES = 20 * 1024 * 1024`，`ponytail:` 注释：源自 Anthropic 32MB/请求上限、base64 ~1.33× 膨胀，留余量。
- 无页数闸（有意，注释注明）。

### provider 层 `provider/anthropic.py` —— document lift（核心）
现状：`_convert_message_content` 遇 tool_result → `_convert_tool_result_content`，后者对未知块 `stringify`（会把 document base64 喷成文本）。

改动：
1. `_convert_tool_result_content`（或新 helper）把 tool_result content 拆成 `(非 document 块列表, document 块列表)`；document 不再落 stringify。
2. `_convert_message_content` 处理 tool_result 时：先 append 转换后的 tool_result 块（content 已去掉 document），随后把每个 lifted document 作为**同一 user 轮次的顶层 block** append：`{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":...}}`。

**为何只在 anthropic.py**：`pdf_in` 仅 Claude → document block 只到 `AnthropicProvider`，OpenAIProvider 永不见。

**缓存/不变量校验**：
- `document` 已在 `_CACHEABLE_BLOCK_TYPES` → lift 后末块若是 document，`_attach_breakpoint` 正常挂断点。
- 带 tool_result 的 user 轮次经 `_is_real_user_turn` 返回 False（含 lifted document 后仍 False）→ 不污染 anchor 判定。无需改动这两处，仅需测试确认。

### 接线 `core/tools.py` + `main.py`
- `get_handlers` 加 `pdf_input_enabled` 参数，绑进 `read_file` partial（与 `image_enabled` 并列）。
- `rebind_workspace_handlers` 经 `_extract_partial_keyword(handler,"pdf_enabled",True)` 保留门控（仿 `image_enabled`）。
- `main.py:_build_handlers` 算 `supports_pdf_input(provider.model, override=config.capabilities.pdf_in)` 透传。
- `CapabilitiesConfig` 加 `pdf_in: bool | None = None`；`_parse_capabilities_config` 解析 `pdf_in`（tri-state，缺省 None）+ env `BAREAGENT_MODEL_PDF_IN`（优先级最高）。

## 关键未知（实施第一步验证，写码前）
lift 把 document 提到「响应工具的 user 轮次」顶层——须实测 Anthropic 是否接受**同一 user 消息内 tool_result 块 + 顶层 document 块混排**。
- 接受 → 按上落地。
- 拒绝 → 退路：把 document 提为**紧随的独立 user 消息**（`_convert_messages` 在该 tool-result 消息后追加一条 user 消息，Anthropic 合并连续同角色）。再验证。
- 两者皆不行 → ④ 受阻，回退仅 pypdf 文本，向 user 说明。

## 兼容性 / 回滚
- 所有新参数默认值保证非图片/非 PDF、无能力路径字节级不变。
- 回滚 = 撤销 diff；无持久状态、无迁移。config 新字段缺省 None 不影响既有配置。
