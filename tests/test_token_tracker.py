from __future__ import annotations

from dataclasses import dataclass

from src.memory.token_tracker import (
    DEFAULT_PRICES,
    TokenTracker,
    resolve_price,
)


@dataclass
class _FakeResponse:
    input_tokens: int
    output_tokens: int


def test_record_accumulates_totals_and_call_count() -> None:
    tracker = TokenTracker()

    tracker.record(_FakeResponse(input_tokens=10, output_tokens=4), "model-a")
    tracker.record(_FakeResponse(input_tokens=5, output_tokens=2), "model-a")

    assert tracker.total_input == 15
    assert tracker.total_output == 6
    assert tracker.total_tokens == 21
    assert tracker.call_count == 2


def test_record_breaks_down_per_model() -> None:
    tracker = TokenTracker()

    tracker.record(_FakeResponse(input_tokens=10, output_tokens=4), "model-a")
    tracker.record(_FakeResponse(input_tokens=7, output_tokens=3), "model-b")
    tracker.record(_FakeResponse(input_tokens=1, output_tokens=1), "model-a")

    usage_a = tracker.per_model["model-a"]
    usage_b = tracker.per_model["model-b"]
    assert (usage_a.input_tokens, usage_a.output_tokens, usage_a.call_count) == (11, 5, 2)
    assert (usage_b.input_tokens, usage_b.output_tokens, usage_b.call_count) == (7, 3, 1)


def test_record_handles_missing_or_none_token_fields() -> None:
    tracker = TokenTracker()

    # A provider that yields None tokens must not crash the tracker.
    tracker.record(_FakeResponse(input_tokens=None, output_tokens=None), "model-a")  # type: ignore[arg-type]

    assert tracker.total_input == 0
    assert tracker.total_output == 0
    assert tracker.call_count == 1


def test_reset_clears_all_state() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=10, output_tokens=4), "model-a")

    tracker.reset()

    assert tracker.total_input == 0
    assert tracker.total_output == 0
    assert tracker.total_tokens == 0
    assert tracker.call_count == 0
    assert tracker.per_model == {}


def test_resolve_price_prefix_matches_builtin_claude_family() -> None:
    # Dated/suffixed model ids resolve to the family price via startswith.
    assert resolve_price("claude-opus-4-8", None) == DEFAULT_PRICES["claude-opus-4"]
    assert (
        resolve_price("claude-sonnet-4-6-20251101", None)
        == DEFAULT_PRICES["claude-sonnet-4"]
    )
    assert (
        resolve_price("claude-haiku-4-5-20251001", None)
        == DEFAULT_PRICES["claude-haiku-4"]
    )


def test_resolve_price_unknown_model_returns_none() -> None:
    assert resolve_price("gpt-4o", None) is None
    assert resolve_price("deepseek-chat", {}) is None


def test_resolve_price_config_overrides_builtin() -> None:
    prices = {"claude-opus-4-8": {"input": 1.0, "output": 2.0}}

    assert resolve_price("claude-opus-4-8", prices) == (1.0, 2.0)


def test_resolve_price_config_adds_new_model() -> None:
    prices = {"deepseek-chat": {"input": 0.27, "output": 1.1}}

    assert resolve_price("deepseek-chat", prices) == (0.27, 1.1)


def test_resolve_price_longest_prefix_wins() -> None:
    prices = {
        "claude": {"input": 9.0, "output": 9.0},
        "claude-opus-4": {"input": 1.0, "output": 2.0},
    }

    assert resolve_price("claude-opus-4-8", prices) == (1.0, 2.0)


def test_estimate_cost_all_priced() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=1_000_000, output_tokens=1_000_000), "m")
    prices = {"m": {"input": 3.0, "output": 15.0}}

    cost = tracker.estimate_cost(prices)

    assert cost is not None
    assert cost == 18.0


def test_estimate_cost_partial_pricing_counts_only_priced_models() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=1_000_000, output_tokens=0), "priced")
    tracker.record(_FakeResponse(input_tokens=1_000_000, output_tokens=0), "unpriced")
    prices = {"priced": {"input": 2.0, "output": 4.0}}

    cost = tracker.estimate_cost(prices)

    assert cost == 2.0


def test_estimate_cost_no_price_returns_none() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=100, output_tokens=50), "mystery-model")

    assert tracker.estimate_cost(None) is None


def test_summary_always_shows_token_counts_even_without_prices() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=120, output_tokens=30), "mystery-model")

    summary = tracker.summary(None)

    assert "Input:  120 tokens" in summary
    assert "Output: 30 tokens" in summary
    assert "Total:  150 tokens" in summary
    assert "Calls:  1" in summary
    # No price -> per-model line tagged, no estimated-cost line at all.
    assert "(no price)" in summary
    assert "Estimated cost" not in summary


def test_summary_shows_dollar_estimate_for_priced_model() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=1_000_000, output_tokens=1_000_000), "m")
    prices = {"m": {"input": 3.0, "output": 15.0}}

    summary = tracker.summary(prices)

    assert "Estimated cost: $18.0000" in summary
    assert "(no price)" not in summary


def test_summary_mixed_priced_and_unpriced_models() -> None:
    tracker = TokenTracker()
    tracker.record(_FakeResponse(input_tokens=1_000_000, output_tokens=0), "priced")
    tracker.record(_FakeResponse(input_tokens=500, output_tokens=10), "unpriced")
    prices = {"priced": {"input": 2.0, "output": 4.0}}

    summary = tracker.summary(prices)

    assert "priced:" in summary
    assert "(no price)" in summary  # the unpriced model is tagged
    assert "Estimated cost: $2.0000" in summary
