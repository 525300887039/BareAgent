from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Built-in prices for the project's default Claude models, in USD per million
# tokens (input, output). DEFAULT_PRICES is a fallback only — prices drift, so
# the authoritative source is the user's [cost.prices] config, which overrides
# and extends these. Prefix-matched (startswith) so dated model ids such as
# "claude-opus-4-8-20251101" still resolve to the family price.
#
# NOTE: prices are reference values as of 2026-06 and MAY CHANGE; override them
# via [cost.prices] in config.toml / config.local.toml to keep them accurate.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}

# Per-model cache *economics* descriptor — a richer view of how a model family
# bills prompt caching, keyed by model-family prefix (longest-prefix matched like
# DEFAULT_PRICES). Replaces the older 2-tuple ``DEFAULT_CACHE_MULTIPLIERS``:
#   - ``read_mult`` / ``write_mult``: cache read / write price relative to the
#     model's base *input* price (only these two feed the /cost estimate today).
#   - ``min_cacheable_tokens``: the shortest prefix a provider will actually
#     cache (descriptive metadata — not yet enforced anywhere).
#   - ``controllable_ttl``: whether the caller can pick the cache TTL (Anthropic's
#     explicit 5m/1h knob) vs auto-caching providers with no TTL control.
# Coverage:
#   - Anthropic (claude): read 0.1x, write 1.25x (5m TTL; 1h's 2x is approximated
#     as 1.25x — see PRD Out of Scope, estimate-only), TTL controllable.
#   - OpenAI GPT-4o / o1 / o3 / o4: cached input billed ~0.5x, no write premium.
#   - OpenAI GPT-5 family: cached input billed ~0.1x (90% off) — the longer
#     ``gpt-5`` prefix wins over ``gpt`` via longest-prefix match.
#   - DeepSeek: cache hits ~0.1x, no separate write premium.
#   - Gemini (OpenAI-compat): cache reads ~0.1x, no write premium.
# Discounts drift with provider versions; the authoritative source is the
# normalized usage fields on each response — these multipliers only feed the
# /cost estimate. Unknown models fall back to the Anthropic-like default
# (read 0.1, write 1.25); cache tokens are only ever populated for providers
# covered here, so the fallback is a conservative estimate, never load-bearing.


@dataclass(frozen=True, slots=True)
class CacheEconomics:
    """How a model family bills prompt caching.

    ``read_mult`` / ``write_mult`` are relative to the model's base *input*
    price and are the only fields the /cost estimate consumes today.
    ``min_cacheable_tokens`` and ``controllable_ttl`` are descriptive metadata
    (smallest cacheable prefix; whether the caller controls the TTL).
    """

    read_mult: float
    write_mult: float
    min_cacheable_tokens: int = 0
    controllable_ttl: bool = False


DEFAULT_CACHE_ECONOMICS: dict[str, CacheEconomics] = {
    "claude": CacheEconomics(0.1, 1.25, min_cacheable_tokens=1024, controllable_ttl=True),
    "gpt": CacheEconomics(0.5, 0.0, min_cacheable_tokens=1024),
    "gpt-5": CacheEconomics(0.1, 0.0, min_cacheable_tokens=1024),
    "o1": CacheEconomics(0.5, 0.0, min_cacheable_tokens=1024),
    "o3": CacheEconomics(0.5, 0.0, min_cacheable_tokens=1024),
    "o4": CacheEconomics(0.5, 0.0, min_cacheable_tokens=1024),
    "deepseek": CacheEconomics(0.1, 0.0),
    "gemini": CacheEconomics(0.1, 0.0, min_cacheable_tokens=2048),
}
# Conservative Anthropic-like fallback (write premium retained so an unknown
# family is never under-billed for cache writes).
_FALLBACK_CACHE_ECONOMICS: CacheEconomics = CacheEconomics(0.1, 1.25)

# Built-in prices are expressed per *million* tokens; convert to per-token.
_PER_MILLION = 1_000_000


def resolve_cache_economics(model: str) -> CacheEconomics:
    """Resolve the :class:`CacheEconomics` descriptor for *model*.

    Longest-prefix match against :data:`DEFAULT_CACHE_ECONOMICS` (so ``gpt-5``
    wins over ``gpt``), falling back to a conservative Anthropic-like default
    when the family is unknown.
    """
    prefix = _longest_prefix_match(model, DEFAULT_CACHE_ECONOMICS.keys())
    if prefix is not None:
        return DEFAULT_CACHE_ECONOMICS[prefix]
    return _FALLBACK_CACHE_ECONOMICS


def resolve_cache_multipliers(model: str) -> tuple[float, float]:
    """Resolve ``(read_mult, write_mult)`` cache price multipliers for *model*.

    Thin wrapper over :func:`resolve_cache_economics` kept for backward
    compatibility with callers/tests that only need the two multipliers.
    """
    eco = resolve_cache_economics(model)
    return (eco.read_mult, eco.write_mult)


def resolve_price(
    model: str,
    prices: dict[str, dict[str, float]] | None,
) -> tuple[float, float] | None:
    """Resolve (input, output) price per million tokens for *model*.

    Lookup order:
    1. User-configured ``prices`` — exact match wins, then longest prefix match.
    2. Built-in :data:`DEFAULT_PRICES` — longest prefix match.

    Returns ``None`` when no price is known (the caller shows token counts only,
    never a fabricated cost).
    """
    if prices:
        exact = prices.get(model)
        if exact is not None:
            resolved = _coerce_price_entry(exact)
            if resolved is not None:
                return resolved
        prefix_match = _longest_prefix_match(model, prices.keys())
        if prefix_match is not None:
            resolved = _coerce_price_entry(prices[prefix_match])
            if resolved is not None:
                return resolved

    builtin_prefix = _longest_prefix_match(model, DEFAULT_PRICES.keys())
    if builtin_prefix is not None:
        return DEFAULT_PRICES[builtin_prefix]
    return None


def _longest_prefix_match(model: str, keys: Any) -> str | None:
    """Return the longest key in *keys* that is a prefix of *model*."""
    best: str | None = None
    for key in keys:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def _coerce_price_entry(entry: dict[str, float]) -> tuple[float, float] | None:
    """Coerce a ``{input, output}`` config dict into an (input, output) tuple."""
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["input"]), float(entry["output"])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(slots=True)
class _ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    call_count: int = 0


@dataclass(slots=True)
class TokenTracker:
    """Process-level accumulator for LLM token usage during a session.

    Records ``input_tokens`` / ``output_tokens`` from each :class:`LLMResponse`
    plus a per-model breakdown. Pure logic with no I/O so it is unit-testable in
    isolation. Reset on session boundaries (``/new`` / ``/clear`` / ``/resume``)
    but not on in-session compaction (``/compact``).
    """

    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    call_count: int = 0
    per_model: dict[str, _ModelUsage] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output + self.total_cache_read + self.total_cache_write

    def record(self, response: Any, model: str) -> None:
        """Accumulate one LLM response's token usage under *model*.

        Reads only the normalized usage fields off the response so it never
        couples to a specific provider's wire shape. ``input_tokens`` is the
        full-price remainder; cache read/write are additive and non-overlapping
        (see ``LLMResponse``), so summing all four gives the true prompt size.
        """
        input_tokens = int(getattr(response, "input_tokens", 0) or 0)
        output_tokens = int(getattr(response, "output_tokens", 0) or 0)
        cache_read = int(getattr(response, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(response, "cache_creation_input_tokens", 0) or 0)

        self.total_input += input_tokens
        self.total_output += output_tokens
        self.total_cache_read += cache_read
        self.total_cache_write += cache_write
        self.call_count += 1

        usage = self.per_model.get(model)
        if usage is None:
            usage = _ModelUsage()
            self.per_model[model] = usage
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.cache_read_tokens += cache_read
        usage.cache_write_tokens += cache_write
        usage.call_count += 1

    def reset(self) -> None:
        """Clear all accumulated usage (session boundary)."""
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.call_count = 0
        self.per_model.clear()

    def estimate_cost(
        self,
        prices: dict[str, dict[str, float]] | None,
    ) -> float | None:
        """Estimate total cost in USD across all priced models.

        Models without a known price are skipped (their tokens still count, but
        contribute no dollars). Returns ``None`` only when *no* recorded model
        has a price, so the caller can suppress the ``$`` line entirely rather
        than print ``$0.00``.
        """
        total = 0.0
        any_priced = False
        for model, usage in self.per_model.items():
            price = resolve_price(model, prices)
            if price is None:
                continue
            any_priced = True
            input_price, output_price = price
            eco = resolve_cache_economics(model)
            read_mult, write_mult = eco.read_mult, eco.write_mult
            total += usage.input_tokens / _PER_MILLION * input_price
            total += usage.output_tokens / _PER_MILLION * output_price
            total += usage.cache_read_tokens / _PER_MILLION * input_price * read_mult
            total += usage.cache_write_tokens / _PER_MILLION * input_price * write_mult
        return total if any_priced else None

    def summary(self, prices: dict[str, dict[str, float]] | None) -> str:
        """Render a human-readable usage summary for the ``/cost`` command.

        Always shows token counts (total input/output/total + call_count +
        per-model breakdown). Priced models show their ``$`` estimate inline;
        unpriced models are tagged ``(no price)``. A total cost line is added
        only when at least one model is priced.
        """
        lines = [
            "Token usage (this session):",
            f"  Input:  {self.total_input:,} tokens",
            f"  Output: {self.total_output:,} tokens",
        ]
        # Only surface the cache line when caching actually happened, so
        # non-cached sessions keep the original compact output.
        if self.total_cache_read or self.total_cache_write:
            lines.append(
                f"  Cache:  {self.total_cache_read:,} read / "
                f"{self.total_cache_write:,} write tokens"
            )
        lines.extend(
            [
                f"  Total:  {self.total_tokens:,} tokens",
                f"  Calls:  {self.call_count}",
            ]
        )

        if self.per_model:
            lines.append("  By model:")
            for model in sorted(self.per_model):
                usage = self.per_model[model]
                price = resolve_price(model, prices)
                if price is None:
                    cost_label = " (no price)"
                else:
                    input_price, output_price = price
                    eco = resolve_cache_economics(model)
                    read_mult, write_mult = eco.read_mult, eco.write_mult
                    model_cost = (
                        usage.input_tokens / _PER_MILLION * input_price
                        + usage.output_tokens / _PER_MILLION * output_price
                        + usage.cache_read_tokens / _PER_MILLION * input_price * read_mult
                        + usage.cache_write_tokens / _PER_MILLION * input_price * write_mult
                    )
                    cost_label = f" — ${model_cost:.4f}"
                cache_label = ""
                if usage.cache_read_tokens or usage.cache_write_tokens:
                    cache_label = (
                        f", {usage.cache_read_tokens:,} cache-read / "
                        f"{usage.cache_write_tokens:,} cache-write"
                    )
                lines.append(
                    f"    {model}: "
                    f"{usage.input_tokens:,} in / {usage.output_tokens:,} out"
                    f"{cache_label} "
                    f"({usage.call_count} calls){cost_label}"
                )

        total_cost = self.estimate_cost(prices)
        if total_cost is not None:
            lines.append(f"  Estimated cost: ${total_cost:.4f}")
            lines.append("  (prices are estimates; override via [cost.prices] in config)")

        return "\n".join(lines)
