# 本地多模态文件读取（图片 / PDF / notebook）

## Goal

扩展 `read_file`，让智能体能"看"本地图片、读 PDF 文本、解析 Jupyter notebook —— 不再只能读
UTF-8 纯文本。ROADMAP 1.3。

## What I already know（代码尽调结论）

- **现状**：`src/core/handlers/file_read.py:run_read` 仅按 UTF-8 读文本（`offset`/`limit` 行切片）。
- **多模态通路 PR5 已铺好（关键复用点）**：
  - `loop.py:_tool_result`（:285）**已支持 `output: str | list[dict]`** —— handler 返回 list[dict]
    内容块会**原样直通**（MCP 多模态走的就是这条）。
  - 内部图片块 = **Anthropic 原生 shape**：`{"type":"image","source":{"type":"base64",
    "media_type":mime,"data":b64}}`（见 `mcp/registry.py:_image_block_or_placeholder`）。
  - **OpenAI provider 已会 lift** 这种图片块（PR5 `_lift_image_blocks`）→ 跨 provider 通用。
  - 工具执行 `output = handler(**call.input)` → `_tool_result(call.id, output)`（loop.py:130/141）
    —— 本地 read handler 返回 list[dict] 即自动走多模态路径，**无需改 loop/provider**。
- **图片 mime 白名单**：png/jpeg/gif/webp（`mcp/registry.py:_SUPPORTED_IMAGE_MIME`），本地读图沿用。
- **optional-extra 模式现成**：`[langfuse]`/`[otel]`/`[lsp]`（pyproject）。PDF 库挂 `[pdf]` extra +
  lazy import + 缺失 graceful，与 lsp 的 multilspy 缺失降级同构。
- **依赖现状**：无 PIL/pypdf/pymupdf。图片(base64)+notebook(json) **零新依赖**；仅 PDF 需要库。

## Requirements（evolving）

- `run_read` 按**扩展名分派**：图片 / PDF / notebook / 其余走现有文本路径。
- **图片**（.png/.jpg/.jpeg/.gif/.webp）：读二进制 → base64 → 返回 list[dict]
  `[text 描述块, image 块(Anthropic 内部 shape)]`；mime 走白名单；超大小上限 → 明确报错（不自动缩放）。
- **PDF**（.pdf）：按页提取文本，支持页范围；库走 optional extra + 缺失 graceful（见 Decision D1）。
- **Notebook**（.ipynb）：json 解析 → 提取 code/markdown cells（+ 输出，长输出截断）为文本。
- 扩展 `read_file` schema：加可选 `pages`（PDF 页范围，如 "1-5"/"3"）；保留 `offset`/`limit`（文本）。
- 错误路径明确：损坏文件 / 不支持的 mime / 超限 / PDF 库缺失 → 友好 Error 文案。
- 单测覆盖：图片块结构 + 白名单 + 超限、notebook 解析 + 输出截断、PDF 文本 + 页范围 + 库缺失降级、
  文本路径回归不破。

## Acceptance Criteria（evolving）

- [ ] 图片读取返回 `[text, image]` 块，image 为 Anthropic 内部 shape，mime 正确，非白名单/超限报错。
- [ ] 文本文件读取行为不变（offset/limit 回归）。
- [ ] Notebook 解析出 code+markdown cells，长输出截断，非法 json 报错。
- [ ] PDF 提取文本 + `pages` 页范围；`[pdf]` extra 未装时返回「安装 bareagent[pdf]」提示而非崩溃。
- [ ] 扩展名分派正确，未知扩展走文本路径。
- [ ] ruff / pytest / pyright 全绿；新行为有测试。

## Definition of Done

- 单测覆盖三类 + 文本回归 + 各错误路径；ruff·pytest·pyright 绿。
- pyproject 加 `[pdf]` extra；CLAUDE.md + read_file schema 描述同步。
- 图片/notebook 零新依赖；PDF 依赖仅在 extra。

## Decision (ADR-lite)

**Context**: PR5 已铺好多模态直通通路（_tool_result/provider），本地读图可零改动复用；图片+notebook
零新依赖，仅 PDF 需库；项目风格为「核心零依赖、能力挂 optional extra」。

**Decisions（已与用户确认，按推荐）**:
- D1 — **PDF 走 optional `[pdf]` extra + pypdf（选项 C）**。纯 Python、MIT、轻量、文本-only；
  lazy import，未装时返回「安装 bareagent[pdf]」友好提示而非崩溃（与 lsp 的 multilspy 缺失降级同构）。
  理由：三类全交付、base 安装零负担、许可干净、与现有 extra 模式一致；复杂版面质量一般但够 MVP。
- D2 — **图片超大小上限 → 明确报错**让用户自行缩放，不自动缩放（自动缩放需 Pillow 新依赖，不值）。
- D3 — **不做 vision 能力探测**。镜像 MCP：provider 负责 lift，非 vision 模型报错是模型侧的事。

**Consequences**: 图片/notebook 零依赖即用；PDF 需 `uv pip install -e ".[pdf]"`；
自动缩放/页渲染为图/vision 降级/表格 OCR 均为后续扩展位。

## Out of Scope（explicit）

- 图片自动缩放/压缩（需 Pillow）；PDF 页渲染为图（需 pymupdf）。
- vision 能力探测 / 按模型降级。
- PDF 表格结构化、OCR 扫描件、加密 PDF 解密。
- 视频/音频；超大文件流式分块多模态。

## Technical Notes

- 关键文件：`src/core/handlers/file_read.py`(分派+三类)、`src/core/schema.py`(read_file schema 加 pages)、
  `pyproject.toml`([pdf] extra)、`CLAUDE.md`、`tests/test_file_read*.py`(新/扩展)。
- 复用参考：`src/mcp/registry.py:_image_block_or_placeholder`（图片块构造 + 白名单 + 超限降级）。
- 图片块 console/tracer 输出：`stringify`/`print_tool_result` 对 list[dict] 会 json-dump，
  base64 可能刷屏 —— 实现时图片块附简短 text 描述，必要时在 console 侧避免打印 base64（镜像 MCP 现状）。
