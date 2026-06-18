# 多 provider 缓存统一抽象层: cache_mode + cache_key + CacheEconomics + Gemini

## Goal

把缓存抽象从「Anthropic 显式断点专用 + 散落的家族倍率」收敛成一个诚实承认「显式 vs 自动」两种缓存范式的统一层，让 `/cost` 计价正确、跨 provider 语义一致，并顺带补上 Gemini 支持。延续 06-18 省 token 三件套（GPT-5 倍率已是其中的 band-aid 修正）。

## What I already know (已对真实代码核实)

- **provider 路由**：`factory.create_provider` 经 `presets.resolve_preset` 分两路——`route="anthropic"` → `AnthropicProvider`；`route="openai"` → `OpenAIProvider`（deepseek/qwen/glm 都走这条 + `base_url`）。`presets.py` 是纯静态 dict，加 provider = 加一个 `ProviderPreset`。
- **Gemini 现状 = 零**：代码库无任何 gemini 痕迹。Gemini 有 OpenAI 兼容端点（`generativelanguage.googleapis.com/v1beta/openai/`），故**大概率复用 `OpenAIProvider` + preset**，不需新 provider 类。
- **缓存用量归一化已就位**：`openai.py:_extract_cached_tokens`（line 662）已抽 OpenAI `prompt_tokens_details.cached_tokens` + DeepSeek `prompt_cache_hit_tokens`，三字段 `input/cache_read/cache_creation` 模型 provider-neutral（06-18 验证）。Gemini OpenAI-compat **是否**在 `cached_tokens` 上报缓存命中**未知**（Google 文档不清晰，原生字段是 `usage_metadata.cached_content_token_count`）。
- **倍率现状**：`token_tracker.DEFAULT_CACHE_MULTIPLIERS` 是 `family → (read_mult, write_mult)` 2-tuple，06-18 已加 `gpt-5` band-aid。缺 `min_cacheable_tokens`/`controllable_ttl` 等维度。
- **cache_key 成本**：`loop.py:_invoke_provider_once`（line 274/314）只传 `messages`/`tools`，无 per-call kwargs。透传 `cache_key` 要穿 `agent_loop`→`_invoke_provider`→`_invoke_provider_once`→两个 `create()`——**最 invasive**。

## Decision (ADR-lite)

**Context**：四项子改进价值/工程量差异大；Gemini 接入深度受「无 API key 无法实测」约束。

**Decision**（用户已确认）：
- **MVP = cache_mode 枚举 + CacheEconomics 描述符 + Gemini preset；缓做 cache_key**（投入产出倒挂，留扩展位）。
- **Gemini = preset + 防御性归一化**，**不**做实测命中验证（无 key，标 out-of-scope）。关键简化：Gemini 缓存读若走标准 `cached_tokens`，现有 `_extract_cached_tokens` 直接接住——**不加投机性 Gemini 专属字段分支**（字段未知时投机代码比依赖标准路径更糟）。
- **cache_mode** = provider `ClassVar` 能力标记（explicit/auto/none），消费点在持有 provider 对象的 `main.py`（`/cost` 头部标注当前 provider 缓存模式）；**不**塞进 `token_tracker`（它只见 model 串、不见 provider，避免 model→provider 反查耦合）。
- **CacheEconomics** = per-model 成本描述符（`read_mult/write_mult/min_cacheable_tokens/controllable_ttl`），`token_tracker` 计价用；`resolve_cache_multipliers` 保留为返回 `(read,write)` 的薄 wrapper（兼容 06-18 既有测试，零churn）。

**Consequences**：
- cache_mode 与 CacheEconomics 有轻微语义重叠（controllable_ttl 也暗示 explicit/auto），但前者是 provider 级事实、后者是 model 级成本，分置合理。
- Gemini 归一化几乎零新代码（靠现有标准字段路径）；若日后实测发现 Gemini-compat 用非标字段，再补分支。
- cache_key 缺席 → OpenAI 路由黏性略低，但自动缓存仍按前缀命中，无功能损失。

## Requirements

- **cache_mode 能力枚举**：`BaseLLMProvider.cache_mode: ClassVar[Literal["explicit","auto","none"]]`（默认 `"none"`）；`AnthropicProvider="explicit"`、`OpenAIProvider="auto"`。`main.py` 的 `/cost` 在有缓存活动时多标一行当前 provider 的缓存模式。
- **CacheEconomics 描述符**：`@dataclass(frozen,slots)` 含 `read_mult/write_mult/min_cacheable_tokens/controllable_ttl`；按 family 前缀最长匹配（复用 `_longest_prefix_match`）；表含 claude/gpt/gpt-5/o1/o3/o4/deepseek/gemini + 保守 fallback。`estimate_cost`/`summary` 改用 economics 的倍率；`resolve_cache_multipliers` 退化为薄 wrapper。
- **Gemini preset**：`presets.py` 加 `gemini`（route=openai、base_url=`https://generativelanguage.googleapis.com/v1beta/openai/`、api_key_env=`GEMINI_API_KEY`、candidate_models 示例）；缓存读经现有 `_extract_cached_tokens` 标准 `cached_tokens` 路径；CacheEconomics 加 gemini 条目（read 0.1 / write 0 / ttl False）。
- **config 示例 + 文档**：`config.toml` 注释或 docs 提一句 gemini 用法；CLAUDE.md provider/缓存小节同步。

## Acceptance Criteria

- [ ] `BaseLLMProvider.cache_mode` 默认 `"none"`，`AnthropicProvider`=`"explicit"`、`OpenAIProvider`=`"auto"`；单测覆盖。
- [ ] `/cost` 在有缓存活动时显示当前 provider 缓存模式标注；无缓存活动时输出与现状一致。
- [ ] `resolve_cache_economics("gpt-5-mini")` → read 0.1 / write 0 / ttl False；`claude-*` → read 0.1 / write 1.25 / ttl True；`gemini-*` → read 0.1 / write 0；未知 model → 保守 fallback；单测覆盖。
- [ ] `resolve_cache_multipliers` 薄 wrapper 仍返回 `(read,write)`，06-18 既有倍率测试不改仍过。
- [ ] `factory.create_provider` 对 `provider.name="gemini"` 路由到 `OpenAIProvider` 且 base_url 为 Gemini OpenAI-compat 端点；单测覆盖（mock client，不打真网络）。
- [ ] Gemini 响应带 `prompt_tokens_details.cached_tokens` 时 `_parse_response` 归一化进 `cache_read_input_tokens`（构造 fake usage 验证，不需真 key）。
- [ ] `enabled=false`/`cache_config=None` 字节级契约不变（既有测试仍过）。

## Definition of Done

- 新增/更新 pytest 单测（cache_mode、CacheEconomics 解析+计价、Gemini 归一化）
- `ruff check` 改动文件干净
- `enabled=false`/`cache_config=None` 字节级契约不变
- CLAUDE.md 缓存小节同步
- Gemini preset 文档/config 示例（若纳入）

## Out of Scope (explicit)

- **cache_key 透传**（穿 4 层 loop+provider 改 `prompt_cache_key`）——投入产出倒挂，缓做留扩展位。
- **Gemini 实测缓存命中验证**（无 API key）；不加投机性 Gemini 专属缓存字段分支。
- 统一断点策略接口（`provider.place_breakpoints()`）——自动缓存三家无断点概念，会是泄漏抽象；断点逻辑保持只活在 `AnthropicProvider`。
- DeepSeek/Gemini 折扣随版本漂移的运行时实时读字段计价（CacheEconomics 描述符已让其变数据修正，足够）。
- Gemini 原生 SDK / 显式 cachedContent API / 1h 显式缓存（MVP 走 OpenAI-compat 自动缓存只读用量）。

## Research / 取舍：四项的价值 vs 工程量

| 项 | 价值 | 工程量 | 备注 |
|---|---|---|---|
| cache_mode 枚举 | 中（展示诚实 + 为预热等留扩展位） | 低 | base + 两 provider 各加类属性 + /cost 分支 |
| CacheEconomics | 中（per-model 计价诚实，消解 band-aid） | 中 | 重构 token_tracker 倍率层 + 计价点 |
| Gemini preset | 真实新能力 | 低-中 | preset 一行；缓存归一化视 OpenAI-compat 上报而定，有未知 |
| cache_key 透传 | **低**（仅改善 OpenAI 路由黏性，自动缓存本就命中） | **高**（穿 4 层 loop+provider） | 投入产出倒挂 |

**推荐 MVP**：cache_mode + CacheEconomics + Gemini preset（**缓做 cache_key**）。

## Technical Notes

- 关键文件：`src/bareagent/provider/{base,factory,presets,openai}.py`、`src/bareagent/memory/token_tracker.py`、`src/bareagent/main.py`（config 穿透）、`src/bareagent/core/loop.py`（仅 cache_key 纳入时）。
- 设计来源：06-18 会话「多 provider 缓存抽象层」追问的接口设计 + 本任务的代码核实。
- 前置：06-18 三件套在 `feat/token-saving-trio` 分支尚未合 main；本任务进实现前需先合 main 再开新分支，或基于该分支续做。
