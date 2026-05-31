# Token 用量追踪与成本展示（/cost 命令）

## Goal

会话期间累计 LLM token 用量并在 `/cost` 命令下展示（token 计数 + 估算费用）。当前
`LLMResponse` 已有 `input_tokens`/`output_tokens` 但被各处丢弃，没有任何累计与展示。
ROADMAP 2.3「简单但实用」。

## What I already know（代码尽调结论）

- **token 字段**：`LLMResponse`（`src/provider/base.py:40`）只有 `input_tokens`/`output_tokens`，
  无 cache/reasoning 字段。Anthropic/OpenAI provider 都已正确填这两个字段。
- **无 prompt caching**：全仓 `cache_control` 0 处命中 → input/output-only 的成本估算对本项目
  **是准确的**（没有 cache read/write 按不同价计的问题）。若将来加缓存，再扩 cache 字段即可。
- **token 在 agent_loop 内部多次消耗**：一个 user turn 内 `agent_loop`（`src/core/loop.py`）
  循环调 LLM 多次。最干净的汇总注入点是 `loop.py:78`（tracer 打 tag 处）之后——**流式与
  非流式都经 `_invoke_provider`，每次响应都过这一点**，单点覆盖。
- **agent_loop 两个调用点**：`src/main.py:1997`（注入式 prompt）与 `:2044`（普通 user turn），
  都要传入 tracker。
- **slash 命令链路**：命令名进 `_SLASH_COMMANDS`（main.py:619）+ `_HELP_TEXT`（:638），
  在 REPL 循环（:1868 起）按 `if text == "/x"` 分发。参考 `/sessions`/`/mcp` 写法。
- **Config**：dataclass 全在 main.py（`Config`@:138，各 section 如 `UIConfig`/`MemoryConfig`）。
  新增 `[cost]` 走新 `CostConfig` dataclass + Config 字段 + 加载逻辑（`_deep_merge` 已支持
  base→local 覆盖）。
- **重置边界**：`/new`/`/clear`（开新会话）、`/resume`（切会话）应重置；`/compact`（同会话压缩）
  不重置。

## Requirements（evolving）

- 新建 `src/memory/token_tracker.py`：`TokenTracker` 累计 `total_input`/`total_output`/
  `call_count` + 按 model 细分；`record(response, model)`、`estimate_cost(prices)`、
  `summary(prices)`。纯逻辑、可单测。
- `agent_loop` 加可选 `token_tracker` 参数，每次 LLM 响应后 `record(response, model_name)`。
- main.py：REPL 启动建 tracker，传两个 agent_loop 调用点；`/new`/`/clear`/`/resume` 重置；
  注册 `/cost` 命令打印 summary。
- `/cost` **总是**展示 token 计数（input/output/total + call_count + 按 model 细分）；
  **费用估算**按定价来源展示（见 Decision D1）。
- 新增 `[cost]` 配置 + config.toml 示例 + CLAUDE.md/help 文档同步。
- 单测覆盖 TokenTracker（累计、细分、估价、无价降级）、agent_loop 汇总、重置语义。

## Acceptance Criteria（evolving）

- [ ] `TokenTracker.record` 正确累计总量与 per-model 细分。
- [ ] `agent_loop` 每次 LLM 调用（流式+非流式）都汇总进 tracker。
- [ ] `/cost` 展示 token 计数 + call_count + per-model；有价时展示 $ 估算。
- [ ] 无定价的 model：`/cost` 仅展示 token，不展示 $（不报错、不臆造价格）。
- [ ] `/new`/`/clear`/`/resume` 后 `/cost` 归零；`/compact` 不影响累计。
- [ ] `[cost]` 配置可被 config.local.toml 覆盖；缺省安全。
- [ ] ruff / pytest / pyright 全绿；新行为有测试。

## Definition of Done

- 单测覆盖 tracker/汇总/重置/估价；ruff·pytest·pyright 绿。
- config.toml 加 `[cost]` 示例段 + CLAUDE.md 段 + `_HELP_TEXT` 同步。
- 无新增第三方依赖。

## Decision (ADR-lite)

**Context**: token 在 agent_loop 内部多次消耗需单点汇总；项目无 prompt caching 故 input/output
即可算准成本；多 provider 渠道价格漂移快，定价来源需权衡开箱可用 vs 维护负担。

**Decisions（已与用户确认，按推荐）**:
- D1 — **混合定价（选项 C）**。仅为项目默认 Claude 系（Opus/Sonnet/Haiku 4.x）内置一份小价表，
  `[cost.prices]` 配置可覆盖/扩展任意 model；未知 model 只显 token 不显 $。内置价旁注明
  「价格可能变动，以 `[cost.prices]` 覆盖为准」。理由：默认模型开箱显 $，漂移面收敛到少数自家
  默认模型，其余渠道按需配，避免为 6+ 漂移渠道维护陈旧价。
- D2 — **不做 bottom_toolbar 实时显示**。`/cost` 命令足够；实时显示触 prompt-toolkit UI 层、
  面更大，留后续扩展位。
- D3 — **重置语义**：tracker 进程级累计，`/new`·`/clear`·`/resume` 归零，`/compact` 不重置；
  `/cost` 展示当前会话累计。
- D4 — **不拆 cache/reasoning token**：本项目无缓存，input/output 即准；加缓存时再扩字段。

**Consequences**: `/cost` 始终诚实展示 token（即便无价）；内置价仅覆盖默认 Claude 模型需偶尔更新；
实时显示/缓存计价/跨会话报表均为后续扩展位。

## Out of Scope（explicit）

- bottom_toolbar 实时显示（除非 D2 改判）。
- cache/reasoning token 拆分计价（本项目无缓存；加缓存时再扩）。
- 跨会话持久化的累计费用统计、按日/按项目汇总报表。
- 自动抓取实时官方价格。

## Technical Notes

- 关键文件：`src/memory/token_tracker.py`(新)、`src/core/loop.py`(汇总注入)、
  `src/main.py`(Config/CostConfig/wiring/`/cost`/重置)、`config.toml`(示例)、
  `tests/test_token_tracker.py`(新)、`tests/test_loop.py`(汇总)。
- summary 文案对齐现有 `/mcp status`/`print_status` 风格。
