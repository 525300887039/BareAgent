# token 省耗三件套: gpt-5 缓存倍率 + 第4断点防回溯 + grep response_format

## Goal

落地调研得出的三个「小改、确定收益」的省 token / 缓存命中率改进，全部复用现有抽象层、不引入新依赖：

1. **修 GPT-5 缓存读倍率**：`/cost` 估算对 GPT-5 系列高估 5 倍。
2. **补第 4 个对话缓存断点（anchor）**：防止重工具调用单 turn 追加 >20 个 block 时，移动断点 20-block 回溯够不到上轮条目 → 静默全 miss。
3. **给 grep 加 `response_format`/`output_mode`**：让 LLM 选只回文件列表而非每行内容，省 token。

## What I already know (已对真实代码核实)

- **件1**：`src/bareagent/memory/token_tracker.py:30-37` `DEFAULT_CACHE_MULTIPLIERS`，现 `"gpt": (0.5, 0.0)` 对 GPT-4o 正确、对 GPT-5/5.1 错（应 0.1×）。`_longest_prefix_match`（line 87）取最长前缀，加 `"gpt-5": (0.1, 0.0)` 会正确压过 `"gpt"`，零副作用。读折扣应用点在 `estimate_cost:196`、`summary:240`。
- **件2**：`src/bareagent/provider/anthropic.py:135 _apply_conversation_breakpoint` 现只在「最后一条消息的最后一个可缓存 block」挂 1 个移动断点。`_build_request_params:99-112` 总断点数 = tools(1)+system(1)+对话(1) = 3，Anthropic 上限 4，**还剩 1 个 slot**。`_CACHEABLE_BLOCK_TYPES`（line 21）已排除 thinking。
- **件3**：`grep` handler `src/bareagent/core/handlers/grep_search.py` 返回 `list[str]`，每个匹配是 `file:line:content`（line 47），上限 `MAX_MATCHES=1000`。schema 在 `src/bareagent/core/tools.py:368-385`（经 `_schema`/`tool_schema` 构造）。
- **修正原调研的过宽范围**：`glob` handler 已只返回纯路径（最小输出，无 detailed 可裁）；MCP 结果来自外部 server，无法重构其返回结构（`mcp/registry.py` 只能截断，已有 `max_result_text_bytes`）。**故 response_format 只对 grep 有真实价值**。

## Requirements (evolving)

- [件1] `DEFAULT_CACHE_MULTIPLIERS` 增加 `gpt-5` 前缀 (0.1, 0.0)，更新注释；补/改单测覆盖 gpt-5 走 0.1×、gpt-4o 仍走 0.5×。
- [件2] `_apply_conversation_breakpoint` 升级为「锚点 + 移动」双断点，总断点用满 4，长 turn 不破缓存。`cache_config=None`/`enabled=false` 路径字节级不变。
- [件3] grep 加 `output_mode`（或 `response_format`）参数，concise 档省 token，默认值保持向后兼容。

## Decision (ADR-lite)

**Context**：三件改进的设计细节需在实现前定死，避免返工。

**Decision**（用户已确认「按推荐继续」）：
- **件1**：`DEFAULT_CACHE_MULTIPLIERS` 加 `"gpt-5": (0.1, 0.0)`，靠 `_longest_prefix_match` 最长前缀压过 `"gpt"`。
- **件2**：`_apply_conversation_breakpoint` 升级为「anchor + 移动」双断点。anchor 放**倒数第二条 user 消息的最后一个可缓存 block**（对标 Roo Code）；移动断点仍在最后一条消息最后一个 block。总断点 = tools+system+anchor+moving = 4（用满 Anthropic 上限）。只有一条 user 消息（或不足以放 anchor）时优雅退化为单移动断点。
- **件3**：grep 加 `output_mode: content|files_with_matches|count`（ripgrep / Claude Code 风格三档），**默认 `content`**（向后兼容 + code agent 通常要看匹配行，端到端省一次 follow-up read）。`files_with_matches` 只回去重文件列表，`count` 回 `file:N` 每文件匹配数。
- **范围**：一个 task，三个独立 commit（互不依赖、分开好回滚）。

**Consequences**：
- 件2 anchor 在 agentic 爆发期间不动 → 稳定缓存前缀覆盖到上个 user turn，正好兜住「单 turn 追加 >20 block」失效场景；代价是多占 1 个断点 slot（本就空着）。
- 件3 默认不变 → 不破坏既有测试/流程；省 token 靠 LLM 主动选 concise 档（schema description 引导）。
- 未触及多 provider 缓存抽象层大重构（留 🟡 中期 task）。

## Acceptance Criteria

- [ ] [件1] `resolve_cache_multipliers("gpt-5-...")` 返回 (0.1, 0.0)；`gpt-4o*` 仍 (0.5, 0.0)；`/cost` 估算相应正确；单测覆盖。
- [ ] [件2] 多 user 消息场景请求体含恰好 4 个 cache_control 断点（tools/system/anchor/moving），anchor 落在倒数第二条 user 消息；单 user 消息场景退化为 3 断点不报错；`enabled=false` / `cache_config=None` 请求体字节级不变；单测覆盖。
- [ ] [件3] grep `output_mode="files_with_matches"` 只回去重文件列表、`"count"` 回 `file:N`、`"content"`（默认）行为不变；非法值优雅处理；单测覆盖三档 + 默认。

## Definition of Done

- 新增/更新 pytest 单测（token_tracker / anthropic 断点 / grep handler）
- `ruff check` 干净（只 format 改动文件，不全树 format）
- CLAUDE.md 相关小节同步（缓存倍率说明、断点数、grep 参数）
- 不破坏 `enabled=false` 字节级一致契约

## Out of Scope (explicit)

- 多 provider 缓存抽象层大重构（`cache_mode` 枚举 / `cache_key` 透传 / `CacheEconomics` 描述符 / Gemini provider）——属 🟡 中期单独 task。
- DeepSeek/Gemini 缓存倍率随版本漂移的运行时读字段计价——同上。
- glob / MCP 的 response_format（代码核实为不适用）。
- repo map / 语义检索（🔵 高工程量，另议）。

## Technical Notes

- 调研来源：本会话四个 research agent 简报（Claude Code 上下文管理、横向 code agent 对比、跨 provider 缓存最佳实践、Anthropic 官方最佳实践）。
- 关键官方事实：Anthropic 断点上限 4、20-block 回溯、tools→system→messages 顺序、读 0.1×/5m 写 1.25×/1h 写 2×。
- Roo Code anchor 模式：断点放最近 2 条 user 消息（倒二条按 user-turn 推进，长 agentic 爆发期间不动 → 稳定缓存前缀覆盖到上个 user turn）。
