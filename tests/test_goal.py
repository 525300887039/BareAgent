from __future__ import annotations

import pytest

from src.core.goal import (
    DEFAULT_MAX_TURNS,
    GoalOutcome,
    GoalState,
    Verdict,
    build_continuation_prompt,
    build_evaluator_prompt,
    build_initial_prompt,
    parse_goal_command,
    parse_verdict,
    run_goal_loop,
)
from src.core.handlers.goal import GOAL_VERDICT_TOOL_SCHEMA, run_goal_verdict

# --- parse_verdict --------------------------------------------------------


def test_parse_verdict_met_true():
    v = parse_verdict({"met": True, "reason": "all green"})
    assert v.met is True
    assert v.reason == "all green"
    assert v.malformed is False


def test_parse_verdict_met_false():
    v = parse_verdict({"met": False, "reason": "lint still failing"})
    assert v.met is False
    assert v.reason == "lint still failing"
    assert v.malformed is False


def test_parse_verdict_string_bool_coercion():
    assert parse_verdict({"met": "true", "reason": ""}).met is True
    assert parse_verdict({"met": "false", "reason": ""}).met is False
    assert parse_verdict({"met": "nope", "reason": ""}).met is False


def test_parse_verdict_missing_met_is_malformed_not_met():
    v = parse_verdict({"reason": "no verdict field"})
    assert v.malformed is True
    assert v.met is False
    assert v.reason == "no verdict field"


def test_parse_verdict_none_met_is_malformed():
    v = parse_verdict({"met": None, "reason": "x"})
    assert v.malformed is True
    assert v.met is False


def test_parse_verdict_non_dict_is_malformed():
    v = parse_verdict(None)
    assert v.malformed is True
    assert v.met is False


def test_parse_verdict_reason_defaults_empty():
    v = parse_verdict({"met": True})
    assert v.reason == ""


# --- parse_goal_command ---------------------------------------------------


def test_parse_goal_command_empty_is_usage():
    cmd = parse_goal_command("")
    assert cmd.action == "usage"
    assert "Usage:" in cmd.message


def test_parse_goal_command_plain_condition():
    cmd = parse_goal_command("all tests pass", default_max_turns=25)
    assert cmd.action == "run"
    assert cmd.condition == "all tests pass"
    assert cmd.max_turns == 25


def test_parse_goal_command_max_turns_override():
    cmd = parse_goal_command("--max-turns 10 lint is clean", default_max_turns=25)
    assert cmd.action == "run"
    assert cmd.max_turns == 10
    assert cmd.condition == "lint is clean"


def test_parse_goal_command_max_turns_without_condition_is_error():
    cmd = parse_goal_command("--max-turns 10", default_max_turns=25)
    assert cmd.action == "error"


def test_parse_goal_command_invalid_max_turns_is_error():
    cmd = parse_goal_command("--max-turns abc do something", default_max_turns=25)
    assert cmd.action == "error"
    assert "Invalid" in cmd.message


def test_parse_goal_command_zero_max_turns_is_error():
    cmd = parse_goal_command("--max-turns 0 do something", default_max_turns=25)
    assert cmd.action == "error"
    assert ">= 1" in cmd.message


def test_parse_goal_command_negative_max_turns_is_error():
    cmd = parse_goal_command("--max-turns -3 do something", default_max_turns=25)
    assert cmd.action == "error"
    assert ">= 1" in cmd.message


def test_parse_goal_command_strips_leading_whitespace_from_dispatch_slice():
    # The REPL passes text[len("/goal"):], so the condition arrives with a
    # leading space; the parser must strip it (not treat it as a flag).
    cmd = parse_goal_command(" all tests pass")
    assert cmd.action == "run"
    assert cmd.condition == "all tests pass"


def test_parse_goal_command_default_max_turns_used():
    cmd = parse_goal_command("do x")
    assert cmd.max_turns == DEFAULT_MAX_TURNS


# --- run_goal_loop --------------------------------------------------------


class _Recorder:
    """Records the prompts passed to run_turn and serves scripted verdicts."""

    def __init__(self, verdicts: list[Verdict]) -> None:
        self._verdicts = list(verdicts)
        self.prompts: list[str] = []
        self.progress: list[str] = []

    def run_turn(self, prompt: str) -> None:
        self.prompts.append(prompt)

    def evaluate(self) -> Verdict:
        # Default to not-met once the script is exhausted (so a too-short script
        # still terminates via max_turns rather than IndexError).
        if self._verdicts:
            return self._verdicts.pop(0)
        return Verdict(met=False, reason="exhausted")

    def on_progress(self, msg: str) -> None:
        self.progress.append(msg)


def test_run_goal_loop_met_first_turn():
    rec = _Recorder([Verdict(met=True, reason="done")])
    state = GoalState(condition="c", max_turns=25)
    outcome, verdict = run_goal_loop(
        state, run_turn=rec.run_turn, evaluate=rec.evaluate, on_progress=rec.on_progress
    )
    assert outcome is GoalOutcome.MET
    assert verdict is not None and verdict.met is True
    assert state.turns_used == 1
    assert len(rec.prompts) == 1
    # First prompt is the initial (kickoff) prompt.
    assert "Work autonomously" in rec.prompts[0]


def test_run_goal_loop_met_after_retries_feeds_back_reason():
    rec = _Recorder(
        [
            Verdict(met=False, reason="step 1 missing"),
            Verdict(met=False, reason="step 2 missing"),
            Verdict(met=True, reason="now done"),
        ]
    )
    state = GoalState(condition="c", max_turns=25)
    outcome, _ = run_goal_loop(state, run_turn=rec.run_turn, evaluate=rec.evaluate)
    assert outcome is GoalOutcome.MET
    assert state.turns_used == 3
    assert len(rec.prompts) == 3
    # Turns 2 and 3 are continuation prompts carrying the prior reason.
    assert "step 1 missing" in rec.prompts[1]
    assert "step 2 missing" in rec.prompts[2]


def test_run_goal_loop_max_turns_stop():
    rec = _Recorder([])  # always not-met
    state = GoalState(condition="c", max_turns=3)
    outcome, verdict = run_goal_loop(state, run_turn=rec.run_turn, evaluate=rec.evaluate)
    assert outcome is GoalOutcome.MAX_TURNS
    assert state.turns_used == 3
    assert len(rec.prompts) == 3
    assert verdict is not None and verdict.met is False


def test_run_goal_loop_malformed_verdict_treated_as_not_met():
    rec = _Recorder([Verdict(met=False, malformed=True), Verdict(met=True)])
    state = GoalState(condition="c", max_turns=5)
    outcome, _ = run_goal_loop(state, run_turn=rec.run_turn, evaluate=rec.evaluate)
    assert outcome is GoalOutcome.MET
    assert state.turns_used == 2


def test_run_goal_loop_run_turn_exception_propagates():
    def boom(_prompt: str) -> None:
        raise KeyboardInterrupt

    state = GoalState(condition="c", max_turns=5)
    with pytest.raises(KeyboardInterrupt):
        run_goal_loop(state, run_turn=boom, evaluate=lambda: Verdict(met=True))
    # The aborted turn still counted before the raise.
    assert state.turns_used == 1


def test_run_goal_loop_evaluate_interrupt_propagates():
    # An interrupt during evaluation (not the turn) must also propagate so the
    # caller can translate it to ABORTED; the turn ran but no verdict was seen.
    def evaluate() -> Verdict:
        raise KeyboardInterrupt

    state = GoalState(condition="c", max_turns=5)
    with pytest.raises(KeyboardInterrupt):
        run_goal_loop(state, run_turn=lambda _p: None, evaluate=evaluate)
    assert state.turns_used == 1


# --- prompt builders ------------------------------------------------------


def test_build_initial_prompt_contains_condition():
    assert "tests pass" in build_initial_prompt("tests pass")


def test_build_evaluator_prompt_mentions_goal_verdict_and_strictness():
    p = build_evaluator_prompt("lint clean")
    assert "goal_verdict" in p
    assert "lint clean" in p


def test_build_continuation_prompt_with_and_without_reason():
    assert "because X" in build_continuation_prompt("because X")
    # No reason -> still a valid, non-empty continuation prompt.
    assert build_continuation_prompt("").strip() != ""


# --- goal_verdict handler -------------------------------------------------


def test_run_goal_verdict_records_into_sink():
    sink: list[Verdict] = []
    out = run_goal_verdict(sink=sink, met=True, reason="ok")
    assert "recorded" in out.lower()
    assert len(sink) == 1
    assert sink[0].met is True
    assert sink[0].reason == "ok"


def test_run_goal_verdict_missing_met_records_malformed():
    sink: list[Verdict] = []
    run_goal_verdict(sink=sink, reason="forgot met")
    assert sink[0].malformed is True
    assert sink[0].met is False


def test_goal_verdict_schema_shape():
    assert GOAL_VERDICT_TOOL_SCHEMA["name"] == "goal_verdict"
    params = GOAL_VERDICT_TOOL_SCHEMA["parameters"]
    assert set(params["required"]) == {"met", "reason"}
    assert params["properties"]["met"]["type"] == "boolean"


# --- [goal] config parsing (main.py wiring) -------------------------------


def test_parse_goal_config_defaults():
    from src.main import GoalConfig, _parse_goal_config

    cfg = _parse_goal_config({})
    assert cfg == GoalConfig()
    assert cfg.max_turns == DEFAULT_MAX_TURNS
    assert cfg.evaluator_model == ""


def test_parse_goal_config_values_and_strip():
    from src.main import _parse_goal_config

    cfg = _parse_goal_config({"max_turns": 8, "evaluator_model": "  claude-haiku-4-5 "})
    assert cfg.max_turns == 8
    assert cfg.evaluator_model == "claude-haiku-4-5"


def test_parse_goal_config_bad_max_turns_falls_back():
    from src.main import _parse_goal_config

    assert _parse_goal_config({"max_turns": "nope"}).max_turns == DEFAULT_MAX_TURNS
    assert _parse_goal_config({"max_turns": 0}).max_turns == DEFAULT_MAX_TURNS
    assert _parse_goal_config({"max_turns": -3}).max_turns == DEFAULT_MAX_TURNS


def test_parse_goal_config_env_override(monkeypatch):
    from src.main import _parse_goal_config

    monkeypatch.setenv("BAREAGENT_GOAL_MAX_TURNS", "12")
    assert _parse_goal_config({"max_turns": 25}).max_turns == 12


# --- _build_goal_provider -------------------------------------------------


def _load_config():
    from pathlib import Path

    from src.main import load_config

    return load_config(Path("config.toml"))


def test_build_goal_provider_empty_reuses_session():
    from src.main import _build_goal_provider

    config = _load_config()
    config.goal.evaluator_model = ""
    sentinel = object()
    assert _build_goal_provider(config, sentinel) is sentinel


def test_build_goal_provider_failure_falls_back(monkeypatch):
    import src.main as main_mod

    config = _load_config()
    config.goal.evaluator_model = "some-model"

    def boom(_cfg):
        raise RuntimeError("nope")

    monkeypatch.setattr(main_mod, "create_provider", boom)
    sentinel = object()
    assert main_mod._build_goal_provider(config, sentinel) is sentinel


def test_build_goal_provider_builds_sibling_with_override_model(monkeypatch):
    import src.main as main_mod

    config = _load_config()
    config.goal.evaluator_model = "cheap-model"
    built = object()
    captured: dict[str, str] = {}

    def fake_create(cfg):
        captured["model"] = cfg.provider.model
        return built

    monkeypatch.setattr(main_mod, "create_provider", fake_create)
    assert main_mod._build_goal_provider(config, object()) is built
    # The sibling provider is built with the evaluator model, not the session one.
    assert captured["model"] == "cheap-model"
