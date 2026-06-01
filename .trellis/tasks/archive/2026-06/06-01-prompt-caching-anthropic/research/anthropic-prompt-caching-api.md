# Research: Anthropic Messages API Prompt Caching (current / mid-2026)

- **Query**: Implementer-facing spec for Anthropic prompt caching — cache_control shape, breakpoint limits, model thresholds, TTL/beta-header status, usage accounting, streaming, pricing; plus OpenAI/DeepSeek auto-caching note.
- **Scope**: external (Anthropic API behavior) + provider docs
- **Date**: 2026-06-01

## Source note (read first)

Primary source for almost every claim below is **Anthropic's own maintained documentation snapshot**, surfaced through the bundled `claude-api` skill (`shared/prompt-caching.md`), which is current to the Opus 4.8 era (mid-2026). That file is a verbatim mirror of the canonical docs page:

- Canonical URL: `https://platform.claude.com/docs/en/build-with-claude/prompt-caching` (older host alias: `https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching`)

**Tooling caveat / honesty flag**: the `context7` and `mcp__exa__*` web-search tools named in the task brief were **not bound in this agent thread** (only Read/Write/Glob/Grep/Bash/Skill were available), so I could not run an independent live web-search cross-check this session. Where a claim rests on the Anthropic skill doc it is high-confidence (that doc IS the authoritative source). The **two items I could not freshly re-verify against a second live source** are explicitly flagged `[VERIFY]` in the Caveats section — both concern the 1h-TTL beta-header history and the nested `cache_creation` usage sub-object. Treat those as "very likely correct, confirm before shipping cost math that depends on the 5m-vs-1h split."

---

## Findings

### 1. `cache_control` block shape + where breakpoints go

- Exact shape (default 5-minute TTL): `{"type": "ephemeral"}`. The only `type` value is `"ephemeral"`.
- A breakpoint is just adding that `cache_control` key onto a **content block**. Valid placements:
  - **`tools`** entries (on a tool definition object) — caches the tool block.
  - **`system`** blocks — `system` must be a **list of content blocks**, e.g. `system=[{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`. A breakpoint on the *last* system text block caches `tools` + `system` together (render order is `tools` -> `system` -> `messages`).
  - **message `content`** blocks: `text`, `image`, `tool_use`, `tool_result`, `document`.
- **Structural requirement confirmed**: to carry `cache_control`, `system` must be a **list of content blocks, not a bare string**. A bare `system="..."` string cannot hold a breakpoint. (Top-level `cache_control={"type":"ephemeral"}` on `messages.create()` is the exception — it auto-places on the last cacheable block and works even with a string system prompt.)
- Source: Anthropic prompt-caching docs (`shared/prompt-caching.md` § API reference, § Placement patterns). High confidence.

### 2. Breakpoint limit + prefix matching

- **Max 4 `cache_control` breakpoints per request.** (Confirmed verbatim: "Max **4** `cache_control` breakpoints per request.")
- Matching model: prompt caching is a **prefix match**. The cache key is the exact bytes of the rendered prompt up to each breakpoint; any single-byte change at position N invalidates all breakpoints at positions >= N.
- **20-block lookback window**: each breakpoint walks backward **at most 20 content blocks** to find a prior cache entry. If a single turn appends >20 blocks (common in agentic tool loops), the next request's breakpoint silently misses. Mitigation: insert an intermediate breakpoint every ~15 blocks in long turns.
- Source: `shared/prompt-caching.md` § API reference, § 20-block lookback window. High confidence.

### 3. Minimum cacheable prefix token thresholds (per model)

Below the minimum, caching is a **silent no-op** — no error, the request just returns `cache_creation_input_tokens: 0` and you pay full price.

| Model | Minimum cacheable prefix |
|---|---:|
| Opus 4.8 / 4.7 / 4.6 / 4.5, Haiku 4.5 | **4096 tokens** |
| Sonnet 4.6, Haiku 3.5, Haiku 3 | **2048 tokens** |
| Sonnet 4.5 / 4.1 / 4, Sonnet 3.7 | **1024 tokens** |

- Note the task's "1024 vs 2048" framing is the **older** Sonnet/Haiku split; the **current Opus tier (4.5+) and Haiku 4.5 require 4096**. So "a 3K-token prompt caches on Sonnet 4.5 but silently won't on Opus 4.8."
- Behavior below threshold: **silent no-op, not an error.**
- Source: `shared/prompt-caching.md` § API reference (per-model minimum table). High confidence.

### 4. TTL options (5m default, 1h)

- **5-minute TTL is the default**: `{"type": "ephemeral"}` with no `ttl` = 5 minutes.
- **1-hour TTL shape**: `{"type": "ephemeral", "ttl": "1h"}`.
- **1h availability**: the Anthropic docs snapshot treats `ttl: "1h"` as a **normal, documented option** with no beta-header gate shown alongside it (the API-reference block lists both 5m and 1h forms with no `anthropic-beta` caveat). This matches the public state where the 1-hour TTL went generally available (it originally launched behind `extended-cache-ttl-2025-04-11` in 2025). **See `[VERIFY]` flag #1** — if your SDK is older, you may still need to pass `extended-cache-ttl-2025-04-11`; on a current SDK no beta header is required for 1h.
- Source: `shared/prompt-caching.md` § API reference. Medium-high confidence on GA (flagged).

### 5. Is prompt caching itself still beta-gated?

- **No — prompt caching is GA. No `anthropic-beta` header is required** to use `cache_control` in the current SDK. Every prompt-caching example in the authoritative skill docs calls plain `client.messages.create(...)` (the non-beta namespace) with `cache_control`, and the README's "Prompt Caching" section uses no `betas=` / `extra_headers` beta value.
- (Contrast: features like compaction, Files API, mid-conversation system messages, task budgets still carry beta headers — prompt caching does **not**.)
- Source: `python/claude-api/README.md` § Prompt Caching, `shared/prompt-caching.md`. High confidence.

### 6. Usage reporting fields + accounting

- Confirmed top-level fields on `response.usage`:
  - **`cache_creation_input_tokens`** — tokens **written** to cache this request (you paid the ~1.25x write premium).
  - **`cache_read_input_tokens`** — tokens **served** from cache this request (you paid ~0.1x).
  - **`input_tokens`** — the **uncached remainder only** (full price).
- **Additive accounting confirmed**: `input_tokens` **EXCLUDES** cached tokens. Total prompt size = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. The three are additive and non-overlapping — critical for correct cost math (don't double-count; don't assume `input_tokens` is the whole prompt).
- **Nested `cache_creation` sub-object** with `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`: this nested breakdown **does exist** in the Anthropic API when you use the 1h TTL (it splits the write tokens by TTL bucket so you can apply 1.25x vs 2x correctly). **However, the bundled skill docs only document the three flat top-level fields** and do not show the nested object. **See `[VERIFY]` flag #2** — confirm the exact nested field names (`cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`) against your installed SDK's `Usage` type before relying on them for the 5m-vs-1h cost split. If you only ever use 5m TTL, the flat `cache_creation_input_tokens` is sufficient.
- Source: `shared/prompt-caching.md` § Verifying cache hits (flat fields, high confidence); nested object from broader Anthropic API knowledge (flagged).
- Per-language accessors: `response.usage.cache_read_input_tokens` (Python/TS/Ruby), `resp.Usage.CacheReadInputTokens` (Go/C#), `.usage().cacheReadInputTokens()` (Java), `$message->usage->cacheReadInputTokens` (PHP).

### 7. Streaming usage

- **`messages.stream()` final message DOES include the cache_* fields.** Get them via `stream.get_final_message().usage` — that `Message` is fully accumulated and carries `cache_creation_input_tokens` / `cache_read_input_tokens` / `input_tokens` just like a non-streaming response.
- **Gotcha**: the incremental `message_delta` stream events carry **output** token usage as it grows, but the **cache_* (input-side) counts are finalized on `message_start` / the final message**, not dripped per-delta. If you read usage off intermediate `message_delta` events and sum, you can **under-report or miss cache usage**. Always read cache_* from `get_final_message().usage` (or from the `message_start` event's `message.usage`), not from the running `message_delta` deltas.
- Source: `python/claude-api/streaming.md` (`get_final_message()`), `shared/prompt-caching.md`. High confidence on the final-message path; the per-delta gotcha is general streaming-API behavior.

### 8. Pricing multipliers (relative to base input price)

- **Cache WRITE (cache_creation)**: **1.25x** base input price for **5-minute** TTL; **2x** for **1-hour** TTL.
- **Cache READ (cache_read)**: **0.1x** (~10% of) base input price.
- Break-even math (from the docs): with 5m TTL, **2 requests** break even (1.25x write + 0.1x read = 1.35x vs 2x uncached). With 1h TTL, you need **>=3 requests** (2x + 0.2x = 2.2x vs 3x uncached).
- Source: `shared/prompt-caching.md` § API reference > Economics. High confidence (verbatim).

### 9. OpenAI & DeepSeek automatic prompt caching (provider comparison)

- **Both are automatic — NO opt-in required.** Neither needs a `cache_control`-style annotation; caching of repeated prefixes happens server-side automatically.
- **OpenAI**: usage field is `usage.prompt_tokens_details.cached_tokens` (count of input/prompt tokens served from cache). Automatic for sufficiently long prompts (historically prompts >= ~1024 tokens, cached in 128-token increments).
- **DeepSeek**: exposes two fields — `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` (the two sum to total prompt tokens). Automatic, no opt-in; cache hits are billed at a steep discount.
- **Key contrast for implementers**: Anthropic is **explicit/opt-in** (you place `cache_control` breakpoints and control TTL); OpenAI and DeepSeek are **implicit/automatic** (no request-side knobs, just read the usage counters). Source: general provider-docs knowledge; **not independently re-verified this session** — low/medium confidence, see `[VERIFY]` flag #3, confirm against current OpenAI and DeepSeek API references if exact field names are load-bearing.

---

## Caveats / Not Found (explicit confidence flags)

- `[VERIFY] #1 — 1h TTL beta header`: The authoritative Anthropic docs snapshot presents `ttl: "1h"` as a plain documented option (GA, no beta header). The historical `extended-cache-ttl-2025-04-11` header was the original gate when 1h launched in 2025. On a current SDK no header is needed; on an older SDK pin it may still be required. Confirm against the installed `anthropic` version if targeting 1h.
- `[VERIFY] #2 — nested cache_creation usage object`: Flat fields (`cache_creation_input_tokens`, `cache_read_input_tokens`, `input_tokens`) are documented and certain. The nested `cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}` breakdown exists in the Anthropic API for TTL-split accounting but is NOT in the bundled skill doc; confirm exact field names in the SDK `Usage` type if your cost math splits 5m vs 1h.
- `[VERIFY] #3 — OpenAI/DeepSeek field names`: Provider-comparison fields are from general knowledge, not re-verified live this session. Confirm `usage.prompt_tokens_details.cached_tokens` (OpenAI) and `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (DeepSeek) against current provider docs before depending on the exact strings.
- **Tooling**: `context7` and `mcp__exa__*` tools from the task brief were unavailable in this thread; could not run the requested independent live cross-check. Everything else rests on Anthropic's own current docs (the `claude-api` skill mirror), which is the canonical source.

### Silent-invalidator audit checklist (implementer bonus, from authoritative doc)

If `cache_read_input_tokens` stays 0 across repeated identical-prefix requests, a silent invalidator is in the prefix:
- `datetime.now()` / `Date.now()` / `time.time()` in the system prompt
- `uuid4()` / request IDs early in content
- `json.dumps(d)` without `sort_keys=True` (non-deterministic key order)
- per-user/session ID interpolated into the system prompt
- conditional system sections (`if flag: system += ...`)
- tools that vary per user/request (tools render at position 0 — any change invalidates everything)

Render order is fixed: **`tools` -> `system` -> `messages`**. Keep stable content first, volatile content after the last breakpoint.
