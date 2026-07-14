# Implement — 执行清单

两子目标独立，可并行；但 ④ **第一步是实测验证**，验证前不写 ④ 的 provider/handler 代码。

## 阶段 0：④ API 验证闸 —— 现状：无 Anthropic key，实测延后
- **决定（user 确认无 key、批准开工）**：不做真实 API 验证，按 design 的**主形态**（document 提到同一 tool-response user 轮次顶层）构建 + 单元测 lift 变换。
- [ ] 代码/交付说明**明确标注**：lift wire 形态未经真实 Anthropic API 验证；user 拿到 key 后跑一次含 PDF 的 read_file 确认；若被拒，切「独立后继 user 消息」形态（design 已列退路，改动小）。
- lift 逻辑本身（document 从 tool_result 移到 user 顶层）纯消息操作，单元测完整覆盖，不受无 key 影响。

## 阶段 1：③ web_fetch 图片（独立，不依赖阶段 0）
- [ ] `core/handlers/web_fetch.py`：`run_web_fetch` 加 `image_enabled=True`；按 Content-Type 分流（图片白名单 / 非白名单 image / 文本）；图片分支读原始 bytes ≤ `_MAX_IMAGE_BYTES+1`、复用 file_read 的 block 形状与 `_IMAGE_DISABLED_ERROR`；白名单取自 `file_read._IMAGE_EXT_TO_MIME` 值集合（单一真源）。
- [ ] `core/tools.py:get_handlers`：`web_fetch` 改 `partial(run_web_fetch, image_enabled=image_input_enabled)`。
- [ ] 测试 `tests/`：mock urlopen/响应头，覆盖 白名单+有能力→image block / 无能力→门控文案 / 非白名单→提示 / 超限→提示 / 文本 Content-Type→字节级不变。

## 阶段 2：④ 能力层 + 配置（阶段 0 通过后）
- [ ] `provider/capabilities.py`：`KNOWN_PDF_INPUT_PREFIXES` + `supports_pdf_input`（照抄 image 结构，未知 deny）。
- [ ] `main.py`：`CapabilitiesConfig.pdf_in`；`_parse_capabilities_config` 解析 `pdf_in` + env `BAREAGENT_MODEL_PDF_IN`。
- [ ] `config.toml [capabilities]` 加 `pdf_in` 注释示例（默认 auto）。
- [ ] 测试：能力表前缀命中/未知 deny/override 三态；config 解析容错。

## 阶段 3：④ handler 分派
- [ ] `core/handlers/file_read.py`：`run_read` 加 `pdf_enabled=True`；`.pdf` 分支按 pages/能力/体积三路分派；`_read_pdf_native` + `_MAX_PDF_BYTES(20MB, ponytail 注释)`；回退时加提示前缀。
- [ ] 测试：有能力+无pages+正常→document block / 有pages→pypdf不变 / 无能力→pypdf无document / 超限→pypdf+提示。

## 阶段 4：④ provider lift（核心）
- [ ] `provider/anthropic.py`：`_convert_tool_result_content` 剥离 document（不再 stringify）；`_convert_message_content` 把 lifted document 顶层重挂（形态依阶段 0 结论）。
- [ ] 测试：tool_result 内 document → 转换后 tool_result 无 document、user 顶层有 document；lift 后 `_apply_conversation_breakpoint` 不抛、`_is_real_user_turn` 仍 False。

## 阶段 5：④ 接线
- [ ] `core/tools.py:get_handlers` 加 `pdf_input_enabled`，绑进 read_file partial；`rebind_workspace_handlers` 经 `_extract_partial_keyword` 保留 `pdf_enabled`。
- [ ] `main.py:_build_handlers` 算 `supports_pdf_input(...)` 透传。
- [ ] 测试：rebind 后门控保留。

## 阶段 6：质量闸（收尾）
- [ ] `bash scripts/ci-check.sh`（ruff check / ruff format --check / pyright / uv run pytest）全绿。
- [ ] `docs` / CLAUDE.md 架构小节按需补一段（vision 门控 + web_fetch 图片 + PDF lift）。

## 回滚点
每阶段独立可回滚（撤 diff，无持久状态）。阶段 0 是硬闸：不过则 ④ 整体不落地，③ 仍可独立交付。
