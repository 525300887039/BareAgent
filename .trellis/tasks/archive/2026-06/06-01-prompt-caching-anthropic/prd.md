# Prompt Caching（Anthropic）

## Goal

为 Anthropic provider 接入 prompt caching：在请求里给静态前缀（tools + system，可能含对话历史前缀）打 `cache_control` 断点，让多轮 agent loop 里反复重发的大块上下文走缓存读（约 0.1× 输入价），大幅降本提速。配套把缓存读/写 token 纳入 `LLMResponse` / `TokenTracker` / `/cost`，保证开缓存后成本展示仍然准确。

## What I already know（已从代码确认）

- `AnthropicProvider._build_request_params`（`src/provider/anthropic.py`）是唯一的请求构造汇聚点：`system` 拼成单字符串、`tools` 由 `_convert_tools` 平铺、`messages` 经 `_convert_messages`。**缓存断点最自然的注入点就在这里，loop / provider 接口零改动。**
- `_parse_response` 当前只读 `usage.input_tokens` / `output_tokens`，**没读** `cache_creation_input_tokens` / `cache_read_input_tokens`。
- `LLMResponse`（`src/provider/base.py`）只有 `input_tokens` / `output_tokens` 两个用量字段。
- `TokenTracker.record`（`src/memory/token_tracker.py`）只 duck-type 读 `input_tokens` / `output_tokens`；`estimate_cost` / `summary` 按全价算输入——**若开缓存而不区分缓存读/写，`/cost` 会高估成本。**
- provider 由 `factory.create_provider` 构造，`thinking_config` 是现成的「config 段 → dataclass → 穿进 AnthropicProvider 构造器」范式，缓存配置照此办理。
- OpenAI/DeepSeek 是**自动缓存**，不需要 `cache_control`；其 usage 暴露 `cached_tokens`（OpenAI `prompt_tokens_details.cached_tokens`）/ `prompt_cache_hit_tokens`（DeepSeek），可选地拿来让 `/cost` 更准。

## Assumptions (temporary)

- 默认对 Anthropic 开启缓存（近乎免费、收益大），可经 config 关闭。
- 断点用 5 分钟 TTL（交互式 agent 的甜区），是否暴露 1h TTL 待定。
- 缓存只针对 Anthropic 原生 provider；OpenAI 兼容端点的自动缓存最多只「读用量」不设断点。

## Requirements

- **缓存断点策略 = 方案 B（静态 + 对话增量）**：tools 数组末尾 + system 末尾 + 最近对话前缀挂 `cache_control` 断点（≤4 个，含可选的重叠冗余断点）。让 agent loop 反复重发的增长历史也走缓存读，捕获多轮工具调用的主要成本。
- **成本核算 = 范围 1（本任务一并修好）**：`LLMResponse` 加 `cache_creation_input_tokens` / `cache_read_input_tokens`，`_parse_response` 填充；`TokenTracker.record` 累计缓存读写；`/cost` summary 展示缓存读/写 token（命中可观测，作为 feature 生效的验收信号）。
- **配置 = provider 中立 `[cache]` 段**：`enabled`（默认 ON；当前仅对 Anthropic provider 注入断点，其它 provider no-op）+ `ttl`（`"5m"` 默认，支持 `"1h"`）。命名不绑 provider 名，便于将来扩展。env 覆盖 `BAREAGENT_CACHE_ENABLED`（对齐既有 env 覆盖范式）。
- **跨 provider 缓存用量归一化 = 纳入（选项 1）**：OpenAI/DeepSeek 自动缓存命中量也读出来计入 `/cost`。关键设计——**在各 provider 的 `_parse_response` 内归一化**，让 `LLMResponse` 三个用量字段语义统一、`TokenTracker` 保持 provider 无关：
  - `input_tokens` = 全价输入（1× 计价的部分）
  - `cache_read_input_tokens` = 折扣读部分
  - `cache_creation_input_tokens` = 写溢价部分（仅 Anthropic 产生，其它 provider 恒 0）
  - **归一化映射**：Anthropic 直接透传（`input_tokens` 本就不含缓存）；OpenAI `input_tokens = prompt_tokens - cached_tokens`、`cache_read = cached_tokens`（cached 是 prompt_tokens 子集）；DeepSeek `cache_read = prompt_cache_hit_tokens`、`input_tokens = prompt_cache_miss_tokens`。
- **缓存计价 = 家族倍率表**：`TokenTracker` 用一张「model 家族前缀 → (read_mult, write_mult)」内置表（复用 `DEFAULT_PRICES` 的前缀匹配套路）：`claude-*`→(0.1, 1.25)、`gpt-*`/openai→(0.5, 0)、`deepseek-*`→(0.1, 0)。`estimate_cost`/`summary`：`cost += cache_read*p_in*read_mult + cache_write*p_in*write_mult`。

## Acceptance Criteria

- [ ] 开启缓存后 Anthropic 请求：`system` 转为含 `cache_control` 的 text block 列表、tools 末尾带断点、最近对话前缀带断点；断点总数 ≤4。
- [ ] Anthropic `_parse_response` 读 `cache_creation_input_tokens` / `cache_read_input_tokens`；流式经 `get_final_message().usage` 同样拿到。
- [ ] OpenAI / DeepSeek `_parse_response`（含 chat / responses / 流式路径）归一化填 `cache_read_input_tokens`。
- [ ] `TokenTracker` 累计缓存读写并按家族倍率正确计价；`/cost` 展示缓存读/写 token。
- [ ] `[cache] enabled = false`（或 env 覆盖）时：Anthropic 请求体与当前**字节级一致**（system 仍是 bare string、无 `cache_control`），全回归通过。
- [ ] 非 Anthropic provider 永不被注入 `cache_control`。
- [ ] 纯逻辑单测覆盖：断点注入位置/数量、关闭兼容、三 provider usage 归一化、家族倍率计价。

## Technical Approach

**1. 断点注入（仅 Anthropic）— `src/provider/anthropic.py`**
- 新增 `CacheConfig` dataclass（`base.py`）：`enabled: bool = True`、`ttl: Literal["5m","1h"] = "5m"`。`AnthropicProvider.__init__` 收 `cache_config`。
- `_convert_messages`：缓存开启时 `system` 返回 **content block 列表**（末块挂 `cache_control`）；关闭时保持当前 bare string 路径（兼容）。
- `_convert_tools`：开启时给**最后一个** tool 挂 `cache_control`（缓存整个 tools 前缀）。
- 对话增量断点：给最近一条消息的最后一个 block 挂 `cache_control`（可选再给前一条 user 消息挂一个做重叠冗余）；helper 注入，关闭时跳过。
- `cache_control` 值：5m=`{"type":"ephemeral"}`、1h=`{"type":"ephemeral","ttl":"1h"}`。断点预算 tools(1)+system(1)+对话(1~2) ≤4。

**2. usage 解析 — `_parse_response`**：读 `usage.cache_creation_input_tokens` / `cache_read_input_tokens`（默认 0）。

**3. `LLMResponse` — `base.py`**：加两字段，默认 0，现有构造点零影响。

**4. OpenAI/DeepSeek 归一化 — `openai.py`**：chat `_parse_response` + responses + 两条流式路径都按上面映射填 `cache_read_input_tokens`（duck-type 读 `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens`，字段 `[VERIFY]` 实现时活验证）。OpenAI provider **不收** `cache_config`（它无法控制缓存，只读用量）。

**5. 计价 — `token_tracker.py`**：`_ModelUsage` 加 `cache_read_tokens`/`cache_write_tokens`；家族倍率表；`estimate_cost`/`summary` 计入缓存读写并展示。

**6. 配置穿透 — `factory.py` + `main.py`**：`[cache]` 解析为 `CacheConfig`（逐字段容错），env `BAREAGENT_CACHE_ENABLED`，factory 仅把 `cache_config` 传 Anthropic。`[cache]` 属 boot 固化 → 配置热重载归入 **restart-required**（随 provider，不进 hot 集）。

## Decision (ADR-lite)

**Context**：BareAgent 是多轮工具调用 agent，每次迭代重发全量 system+tools+历史，输入成本高；Anthropic 缓存需显式 `cache_control`，OpenAI/DeepSeek 自动缓存但口径不同。
**Decision**：方案 B（静态+对话增量断点，仅 Anthropic 注入）；provider 内归一化缓存用量使 `TokenTracker` 保持 provider 无关；家族倍率表计价；`[cache]` provider 中立配置默认 ON、5m TTL（支持 1h）。
**Consequences**：agent loop 主要输入成本走 0.1× 读，显著降本；`/cost` 全 provider 缓存可观测。代价：OpenAI/DeepSeek usage 字段需活验证；1h 写溢价按 5m 1.25× 近似（estimate 容许）；`system` 在开缓存时结构由 string 变 block list（关闭时不变，保证兼容）。

## Out of Scope (explicit)

- 1h 写溢价的精确 5m/1h 拆分计价（用 nested `cache_creation.ephemeral_*` 字段）——MVP 写溢价统一按 1.25× 近似，文档说明。
- 更激进的断点策略（按 model 动态阈值、thinking block 缓存、per-tool 细粒度断点）。
- 缓存命中率实时 bottom-toolbar 显示（只在 `/cost` 展示）。
- 配置热重载缓存开关（boot 固化，随 provider 走 restart-required）。
- 跨会话缓存预算 / 命中率告警。

## Definition of Done

- 新增行为有 pytest 覆盖（断点注入 / usage 解析 / 成本核算 为纯逻辑可单测）。
- ruff / pyright clean，pytest 全绿。
- 不引入新依赖（anthropic SDK 已在依赖内）。
- config.toml 文档化新配置段；CLAUDE.md 架构段补一节。

## Technical Notes

- 关键文件：`src/provider/anthropic.py`（断点注入 + usage 解析）、`src/provider/base.py`（`LLMResponse` 字段 + 可能的 `CacheConfig`）、`src/memory/token_tracker.py`（缓存计价）、`src/provider/factory.py`（穿配置）、`src/main.py`（`CacheConfig` 解析 + `/cost` 展示）。
- 参照 `ThinkingConfig` 的 config→dataclass→provider 穿透范式。

## Research References

- [`research/anthropic-prompt-caching-api.md`](research/anthropic-prompt-caching-api.md) — Anthropic prompt caching 当前 API（GA 无 beta header；`cache_control:{"type":"ephemeral"}`；system 须为 block 列表；最多 4 断点 + 20 block 回溯；Opus 4.5+ 最小前缀 4096 静默 no-op；usage `cache_creation_input_tokens`/`cache_read_input_tokens` 与 `input_tokens` 相加；写 1.25×/读 0.1×；流式从 `get_final_message().usage` 读）。
