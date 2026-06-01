# ROADMAP 4.2 LLM 重试策略：指数退避 + 可重试分类

## Goal

让 BareAgent 在遭遇**瞬时性** LLM 调用失败（rate limit / 网络超时 / 5xx / overloaded）时自动重试（可配置指数退避 + 抖动），并把**不可重试**错误（认证失败 / 400 bad request / 模型不存在）立即上抛——无需用户手动重试，同时不掩盖真正的配置错误。

## What I already know（探查结论）

- **唯一汇聚点**：`src/core/loop.py:_invoke_provider`（line 179）是 `provider.create()`（非流式）+ `provider.create_stream()`（流式）的唯一调用处。重试包在这一层即可同时覆盖两条路径。
- **现状无任何重试**：全仓库无 retry/backoff 逻辑。`agent_loop`（loop.py:57-79）捕获 provider 异常 → 直接 `raise LLMCallError`。
- **SDK 自带 HTTP 层重试**：`anthropic.Anthropic` 与 `openai.OpenAI` 默认 `max_retries=2`，会对连接错误 / 429 / 5xx 自动退避重试。若 app 层再叠一层 → 实际请求数 = 2×N（复合放大），且用户看到的「attempt 2/3」与底层真实 HTTP 次数对不上。
- **第三条 ROADMAP 目标已实现**：「部分失败恢复（多工具调用某个失败不影响其他）」在 `loop.py:150-155` 已落地（每个 tool call 异常被捕获转 error result，不影响兄弟调用）。本任务不重复造。
- **异常形态**：provider 抛 SDK 专有异常（`anthropic.RateLimitError` / `openai.APIConnectionError` 等，均带 `.status_code` 或可按类名识别），OpenAI responses 流式路径另抛裸 `RuntimeError`（无 status_code）。
- **配置模式**：`Config` dataclass（main.py:155）+ 各 `*Config` 子段 + `_parse_*_config` + config.toml 注释默认值 + `_resolve_bool/int` 环境变量覆盖。`[cost]` / `[memory]` 是现成模板。
- **agent_loop 调用点**：main.py:2153 与 2202（注入路径 + 普通 user-turn）。subagent.py 另有调用。

## Requirements

- 纯 `src/core/retry.py`：`RetryPolicy` 数据类 + `classify`（可重试判定）+ `compute_delay`（指数退避 + 抖动 + 上限）+ 重试驱动（注入 sleep/rng，可单测，无 LLM/loop 依赖）。
- 分类**provider 无关**：duck-typing 看 `status_code` 属性 + 异常类名，不 `import anthropic/openai`。
  - 可重试：408 / 409 / 429 / 500 / 502 / 503 / 504 / 529 + 连接/超时类异常（`APIConnectionError` / `APITimeoutError` / `InternalServerError` / `OverloadedError` 等按类名）。
  - 不可重试：400 / 401 / 403 / 404 / 413 / 422 + **未知异常**（保守 fail-fast，不盲目重试神秘错误）。
- 退避：`delay = min(max_delay, base_delay * multiplier^(attempt-1))`，full jitter `uniform(0, delay)`，可关。
- 重试间隙 `console.print_status` 告知用户（attempt N/M + 延迟 + 错误类型），tracer 打 tag；耗尽后仍 `raise LLMCallError`（向后兼容）。
- 关掉 SDK 自带重试（provider 构造 client 时 `max_retries=0`），app 层独占重试——单层、可配、可观测、无复合放大。
- `[retry]` 配置段 + 环境变量覆盖（至少 enabled / max_attempts）。
- `KeyboardInterrupt` / 非 `Exception` 立即上抛，不重试。

## Acceptance Criteria

- [ ] `classify` 对每个状态码 + 连接/超时类名 + 未知异常分类正确（单测覆盖）。
- [ ] `compute_delay` 单调递增、封顶 max_delay、jitter 边界正确（seeded rng）。
- [ ] 驱动：首次成功不重试 / 重试后成功 / 耗尽后抛原错 / 不可重试立即抛 / KeyboardInterrupt 透传 / on_retry 回调参数正确 / sleep 按计算延迟调用。
- [ ] `_invoke_provider` 在 retryable 错误上重试（流式+非流式），不可重试立即上抛。
- [ ] `[retry]` 配置解析 + 环境变量覆盖；坏值降级默认不崩 boot。
- [ ] provider client `max_retries=0`，app 层独占重试。
- [ ] pytest 全绿、ruff clean、pyright 0、无新依赖。

## Definition of Done

- 新行为有 pytest 测试（test_retry.py + loop 集成点）。
- lint / typecheck / 测试全绿。
- CLAUDE.md「核心智能体循环」或新段记录重试机制；config.toml 注释默认值。

## Technical Approach（待用户确认的决策见下）

包重试驱动于 `_invoke_provider`，策略/分类/退避抽到纯 `retry.py`。`agent_loop` 增 `retry_policy` 可选参数（None = 旧行为，向后兼容）。provider 构造时 `max_retries=0` 让 app 独占重试。

## Decision (ADR-lite)

**Context**: 仓库无重试；SDK 自带 HTTP 层重试（max_retries=2）若不处理会与 app 层复合放大；分类需跨 provider。

**Decision**（用户已确认全部推荐）:
- **D1** app 层在 `_invoke_provider` 独占重试，provider 构造 SDK client 设 `max_retries=0`（单层、可配、可观测、无 2×N 复合；`enabled=false` 即真正无重试）。
- **D2** 纯 `retry.py` duck-typing 看 `status_code` + 异常类名，不 import anthropic/openai。可重试 408/409/429/500/502/503/504/529 + 连接/超时类名；不可重试 400/401/403/404/413/422 + 未知异常（保守 fail-fast）。
- **D3** `delay = min(max_delay, base_delay × multiplier^(attempt-1))` + full jitter；默认 max_attempts=3 / base_delay=1.0 / max_delay=30 / multiplier=2.0 / jitter=on；注入 sleep+rng 可单测。
- **D4** 不解析 Retry-After（Out of Scope）。
- **D5** 重试包整次 `_invoke_provider`，接受流式重试 StreamPrinter 重启（mid-stream 续传 Out of Scope）。
- **D6** 子代理继承 retry_policy（透传进 subagent.py 的 agent_loop 调用）。
- **D7** `[retry]` 段 enabled/max_attempts/base_delay_sec/max_delay_sec/multiplier/jitter；env 覆盖 enabled + max_attempts；坏值降级默认不崩 boot。

**Consequences**: 单一可观测重试层；`enabled=false` 时完全无重试（含 SDK，显式）；流式 mid-stream 失败罕见场景可能重打印（已知限制）；向后兼容（`retry_policy=None` = 旧直抛行为）。

## Out of Scope

- 流式 mid-stream 续传 / 去重（transient 错误绝大多数在首 token 前；MVP 重试整次调用，重试时 StreamPrinter 重启，已打印的局部文本可能重现——罕见，记为已知限制）。
- Retry-After 响应头精确解析（除非用户要；见 D4）。
- 工具调用级重试（tool handler 失败重试）——非本任务，第三条目标已另有实现。
- 熔断 / 限流令牌桶 / 跨会话重试预算。
- Team 自治守护路径（`src/team/autonomous.py:AutonomousAgent._run_prompt`）未接 `retry_policy`——独立于 `run_subagent`（D6 仅覆盖 subagent.py 链），向后兼容无回归，记为后续扩展位。
- Retry-After 响应头解析（D4 已定不做）。

## Technical Notes

- 关键文件：`src/core/retry.py`(新)、`src/core/loop.py`(`_invoke_provider` + `agent_loop` 参数)、`src/provider/{anthropic,openai}.py`(`max_retries=0`)、`src/main.py`(`RetryConfig` + 解析 + 注入两处调用点)、`src/planning/subagent.py`(可选透传)、`config.toml`、`tests/test_retry.py`(新)。
- `time.sleep` 可被 Ctrl+C 中断；驱动只重试 `Exception` 子类，`BaseException` 透传。
