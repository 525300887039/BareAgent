# 多模态增强 web_fetch 图片与 PDF 原生 document block

## Goal

补齐两个相互独立的多模态短板，均复用上一任务（07-14-multimodal-vision-attach）落成的 vision 能力门控机制：

- **③ web_fetch 图片支持**：URL 指向 `image/*` 时返回 image block（当前只走文本路径，无法「看图」）。
- **④ PDF 原生 document block**：`read_file` 读 PDF 时，provider 支持原生 PDF 版面理解则发 Anthropic `document` block，否则回退现有 pypdf 文本抽取（图表/扫描件/版面信息此前全丢）。

## Baseline（已就位，无需重做）

- **图片直通通路**：`loop.py:_tool_result` 对 handler 返回的 `list[dict]` **原样透传**为 tool_result content（`_read_image` 已用此返回 `[text, image]` 两块）；Anthropic/OpenAI 两 provider 图片已打通。
- **vision 门控**：`provider/capabilities.py` 的 `supports_image_input(model, *, override)`（静态前缀表 + `env BAREAGENT_MODEL_IMAGE_IN > [capabilities] image_in > 表`，未知模型 fail-safe deny）。`main.py:_build_handlers` 已算好 `image_input_enabled` 透传给 `run_read`（`image_enabled` 参数）；`_IMAGE_DISABLED_ERROR` 是无能力时的友好回退文案。
- **图片白名单 + 上限**：`file_read.py:_IMAGE_EXT_TO_MIME`（png/jpeg/gif/webp）+ `_MAX_IMAGE_BYTES`（5 MiB）+ block 形状，③ 直接复用。
- **PDF 文本路径**：`file_read.py:_read_pdf` 走 pypdf（`[pdf]` extra、lazy import、`pages` 选页），④ 保留为回退。
- **document 可缓存**：`anthropic.py:_CACHEABLE_BLOCK_TYPES` 已含 `"document"`。

## ③ web_fetch 图片支持

### Requirements
- `run_web_fetch` 检测响应 `Content-Type`：
  - 命中已支持白名单（png/jpeg/gif/webp，对齐 `_IMAGE_EXT_TO_MIME` 的 mime 值）→ 返回 `[text, image]` block（复用 `_read_image` 的 block 形状与 base64 编码）。
  - 非白名单 image 类型（svg/bmp/tiff 等）→ 友好文本提示「建议下载后处理」，不崩、不返 image block。
  - 图片体积超 `_MAX_IMAGE_BYTES`（5 MiB）→ 友好文本提示（建议下载后自行处理），不崩。
- **门控**：模型无 vision 能力时，图片分支返回门控错误（复用 `_IMAGE_DISABLED_ERROR` 或等价文案），绝不把 image block 发给无能力模型。为此 `run_web_fetch` 需接收 `image_enabled: bool = True`（默认 True 保现有调用字节级不变），由 `get_handlers` 用 `image_input_enabled` 绑进 `web_fetch` partial（当前 `web_fetch` 是裸函数，需改为 partial）。
- **字节读取**：当前 `run_web_fetch` 只读 `max_length * 4` 字节按文本解码；图片分支须在读取前按 Content-Type 分流，图片路径读原始 bytes 至 `_MAX_IMAGE_BYTES + 1`（探测超限），文本路径行为不变。
- 返回类型由 `str` 放宽为 `str | list[dict]`（与 `run_read` 一致，`_tool_result` 已支持）。

### 设计决策
- `_IMAGE_EXT_TO_MIME` 的**值集合**（mime 白名单）与 web_fetch 共用一个真源，避免图片格式白名单两处漂移——从 `file_read` 导出复用，或抽到公共位置（实现阶段择一，倾向前者最省）。
- web_fetch 无扩展名可依，白名单判定走 Content-Type 的 mime 主串（忽略 `; charset=` 等参数）。

### Acceptance Criteria
- [ ] Content-Type 为 `image/png|jpeg|gif|webp` 且模型有 vision → 返回含 image block 的 `list[dict]`。
- [ ] 模型无 vision 能力 → 返回门控错误文案，不含 image block。
- [ ] 非白名单 image 类型 / 超 5 MiB → 返回友好文本提示，不抛异常。
- [ ] 非图片 Content-Type（html/text/json 等）→ 行为与现状字节级一致。
- [ ] 新增 pytest 覆盖上述分支（mock urlopen/响应头，无需真实网络）。

## ④ PDF 原生 document block

> **已定**（user 确认）：#1 document block **不能**嵌在 tool_result 内 → 采用 **Anthropic 侧 document 提升**（lift，见下）；#2 **不做**页数上限。

### 交付机制：Anthropic 侧 document lift
`read_file` 只能经 tool_result 回灌，而 document block 不被 tool_result 接受 → 仿 `openai.py:_lift_image_blocks`（图片不能待在 OpenAI tool role → 提升到 user 消息）做一个 **Anthropic 侧对称件**：`read_file` 照常返回 `[text, document]` 进 tool_result，`anthropic.py:_convert_message_content` 在转换 user 消息时把 tool_result 内的 `document` block **提出来**，作为**同一 user 轮次的顶层 content block**重挂在该 tool_result 块之后（tool_result 只留文本占位）。落点：`_convert_tool_result_content` 当前对未知块 `stringify`（会把 document 的 base64 喷成文本）——改为把 document 块从 tool_result content 中剥离、交给 `_convert_message_content` 顶层重挂。因 `pdf_in` 仅允许 Claude，document block **只会**到达 `AnthropicProvider`，故 lift 只需存在于 `anthropic.py`（OpenAIProvider 永不见 document）。

### Requirements
- 扩展能力门控加 **`pdf_in`**：`capabilities.py` 新增 `KNOWN_PDF_INPUT_PREFIXES`（**仅 Claude 家族** `claude-3`/`claude-opus-4`/`claude-sonnet-4`/`claude-haiku-4`——原生 base64 PDF document block 是 Anthropic 特性）+ `supports_pdf_input(model, *, override)`，未知模型 fail-safe **deny**（回退 pypdf，绝不把 document block 发给不支持的 provider）。
- `read_file` PDF 路径分派（`run_read` 加 `pdf_enabled: bool = True`，默认 True 但仅当能力允许时由上层传 True）：
  - **显式传了 `pages`** → 恒走 pypdf 文本路径（用户明确要选页，原生 block 不支持选页）。
  - **未传 `pages` 且 `pdf_enabled` 且体积未超限** → 返回 `[{"type":"text","text":"<label>"}, {"type":"document","source":{"type":"base64","media_type":"application/pdf","data":<b64>}}]`（base64 无换行，`b64encode` 天然满足），交由 anthropic lift 提升。
  - **未传 `pages` 但无能力 / 超限** → 回退 pypdf 文本路径 + 一行提示（说明为何回退）。
- **门控接线**：`main.py:_build_handlers` 用 `supports_pdf_input(provider.model, override=config.capabilities.pdf_in)` 算 `pdf_input_enabled`，经 `get_handlers` 绑进 `read_file` partial（与 `image_enabled` 并列）；worktree `rebind_workspace_handlers` 同样保留 `pdf_enabled`（隔离不丢门控，仿现有 `image_enabled` 处理，经 `_extract_partial_keyword`）。
- **配置**：`[capabilities] pdf_in`（tri-state：absent/malformed→None=auto 查表；bool 强制）+ `env BAREAGENT_MODEL_PDF_IN`，优先级 `env > config > 静态表`（复刻 `image_in` 的 `_parse_capabilities_config` 容错，缺省 None 故不用 `_resolve_bool`）；restart-required（不进热重载）。

### 设计决策
1. **体积上限**：Anthropic 限制 **32 MB / 请求**（base64 膨胀 ~1.33×）。原始 PDF 硬上限取**保守 20 MB**（留余量给同请求其它内容），超限回退 pypdf + 提示；`ponytail:` 注释标注上限来源与调整点。
2. **不做页数上限**（user 已定）：600/100 页天花板极少触及，无 pypdf 无法廉价数页、为数页拉起 pypdf 会抵消收益。超限时由 Anthropic 报错（可接受）。注释注明这是有意取舍。
3. **label 文本**：仿 `_read_image` 的 `Image ...` 描述块，给 document 前置一块 `PDF <name> (<size> bytes, native document)` 文本，便于模型/日志识别。

### Acceptance Criteria
- [ ] Claude 模型 + 未传 pages + 体积正常 → `read_file` 返回含 `document` block 的 `list[dict]`；经 anthropic provider 后 document 被提升到 user 消息顶层、tool_result 内不残留 document。
- [ ] 传了 `pages` → 走 pypdf 文本路径（现有行为字节级不变）。
- [ ] 非 Claude / OpenAI 兼容模型（pdf_in deny）→ 走 pypdf，**不产出 document block**。
- [ ] 超 20MB 上限 → 回退 pypdf + 提示。
- [ ] `env BAREAGENT_MODEL_PDF_IN` / `[capabilities] pdf_in` 覆盖生效（allow/deny）。
- [ ] lift 后 `_apply_conversation_breakpoint` 不破坏缓存断点（`document` 已在 `_CACHEABLE_BLOCK_TYPES`）。
- [ ] 新增 pytest 覆盖：能力表、handler 分派、anthropic lift（tool_result 内 document → user 顶层）。

## 关键风险 / 需验证（实施阶段）
- **[高·实施第一步] lift 后的 document 是否被 Anthropic 接受**：document block 已确认不能在 tool_result 内；lift 把它提到「响应工具的 user 轮次」的顶层 content。须**用最小 base64 PDF 打真实 Anthropic API 验证**该形态被接受：
  - 若接受 → 按本 PRD 落地。
  - 若「工具响应 user 轮次不能混入 document 顶层块」→ 退而把 document 提升为**紧随其后的独立 user 消息**（Anthropic 合并连续同角色消息），再验证；仍不行则 ④ 受阻并向 user 说明。
- **[低] 缓存断点**：`document` 已在 `_CACHEABLE_BLOCK_TYPES`，跑一次带缓存请求确认 `_apply_conversation_breakpoint` 不异常即可。

## Constraints
- 遵循仓库既有范式：fail-open、`_parse_*_config` 逐字段容错不崩 boot、新增行为补 pytest。
- 无能力时**绝不**把 image/document block 发给对应 provider（fail-safe deny）。
- 默认参数保持现有调用字节级不变（`image_enabled`/`pdf_enabled` 默认 True，非图片/非 PDF 路径零改动）。
- 完成后跑 `scripts/ci-check.sh` 同款四步：`ruff check` / `ruff format --check` / `pyright` / `uv run pytest`。

## Out of Scope
- 视频、截图工具、URL 指向 PDF 的 web_fetch 支持。
- **用户主动**携带 image/document 输入（如 `/attach` 一份 PDF 直接进对话）——④ 的 lift 只覆盖 `read_file` 工具路径产出的 document 提升，不含用户侧直接附带。
- PDF 原生路径的选页（选页恒走 pypdf）。
- per-model 能力 dict、video_in/audio_in 等其它 kind。
- Files API 上传 file_id 引用 document（本任务只做 base64 inline）。
