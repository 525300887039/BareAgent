from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import tomllib
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from src.concurrency.background import BackgroundManager
from src.concurrency.scheduler import Scheduler, SchedulerError
from src.core.context import PLAN_MODE_DIRECTIVE, assemble_system_prompt
from src.core.fileutil import (
    atomic_write_text,
    generate_random_id,
    is_tool_result_message,
)
from src.core.fileutil import (
    optional_string as _coerce_optional_string,
)
from src.core.goal import (
    DEFAULT_MAX_TURNS,
    GoalOutcome,
    GoalState,
    Verdict,
    build_evaluator_prompt,
    parse_goal_command,
    run_goal_loop,
)
from src.core.handlers.bash import run_bash
from src.core.handlers.goal import GOAL_VERDICT_TOOL_SCHEMA, run_goal_verdict
from src.core.handlers.plan import (
    EXIT_PLAN_MODE_TOOL_SCHEMA,
    PlanDecision,
    run_exit_plan_mode,
)
from src.core.handlers.skill import SKILL_CREATE_TOOL_SCHEMA, run_skill_create
from src.core.handlers.subagent_send import SUBAGENT_SEND_TOOL_SCHEMA, run_subagent_send
from src.core.handlers.workflow import WORKFLOW_TOOL_SCHEMA, run_workflow_tool
from src.core.loop import LLMCallError, agent_loop
from src.core.retry import RetryPolicy
from src.core.tools import get_handlers, get_tools
from src.core.workflow import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_NODES,
    NodeResult,
    WorkflowNode,
    build_node_prompt,
)
from src.debug.interaction_log import InteractionLogger
from src.hooks import (
    HookConfigError,
    HookEngine,
    HooksConfig,
    parse_hooks_config,
)
from src.lsp import (
    LanguageServerManager,
    LSPConfig,
    LSPError,
    parse_lsp_config,
)
from src.mcp import MCPCallError, MCPConfig, MCPError, MCPManager, parse_mcp_config
from src.mcp.registry import _flatten_content as _mcp_flatten_content
from src.memory.compact import Compactor
from src.memory.conversation_io import parse_import, render_markdown, to_export_json
from src.memory.persistent import (
    MemoryManager,
    build_forget_instruction,
    build_remember_instruction,
    resolve_memory_root,
)
from src.memory.token_tracker import TokenTracker
from src.memory.transcript import TranscriptManager
from src.permission.guard import (
    PermissionGuard,
    PermissionMode,
    permission_rule_subject,
)
from src.permission.rules import parse_permission_rules
from src.planning.agent_types import BUILTIN_AGENT_TYPES, DEFAULT_AGENT_TYPE
from src.planning.skill_gen import SkillGenConfig, SkillGenerator, render_reflection_prompt
from src.planning.skill_store import (
    SkillStore,
    SkillStoreError,
    resolve_generated_skills_root,
)
from src.planning.skills import LOAD_SKILL_TOOL_SCHEMAS, SkillLoader, resolve_skills_dir
from src.planning.subagent import run_subagent
from src.planning.subagent_registry import ResumableContext, SubagentRegistry
from src.planning.tasks import TaskManager
from src.planning.todo import TodoManager
from src.provider.base import (
    VALID_CACHE_TTLS,
    VALID_THINKING_MODES,
    BaseLLMProvider,
    CacheConfig,
    ThinkingConfig,
)
from src.provider.factory import create_provider
from src.provider.setup import run_setup_wizard
from src.team.autonomous import AutonomousAgent
from src.team.mailbox import Message, MessageBus
from src.team.manager import TeammateManager
from src.team.protocols import Protocol, ProtocolFSM, decode_protocol_content
from src.ui.console import AgentConsole

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
VALID_PERMISSION_MODES = {m.value for m in PermissionMode}
VALID_SUBAGENT_TYPES = set(BUILTIN_AGENT_TYPES)
MAIN_AGENT_NAME = "main"
DEFAULT_API_KEY_ENV_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_SESSION_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S-%f"


@dataclass(slots=True)
class ProviderConfig:
    name: str
    model: str
    api_key_env: str
    api_key: str | None = None
    base_url: str | None = None
    wire_api: str | None = None


@dataclass(slots=True)
class PermissionConfig:
    mode: str
    allow: list[str]
    deny: list[str]


@dataclass(slots=True)
class UIConfig:
    stream: bool
    theme: str


@dataclass(slots=True)
class SubagentConfig:
    max_depth: int
    default_type: str
    # Soft cap on resumable foreground subagent contexts held in the
    # session-scoped registry; registering past it evicts the oldest. Config-only
    # (no env override), restart-required.
    max_resumable: int = 20


@dataclass(slots=True)
class DebugConfig:
    enabled: bool = False
    log_dir: str = ".logs"
    viewer_port: int = 8321
    pretty: bool = True


@dataclass(slots=True)
class TracingConfig:
    langfuse: bool = False
    opentelemetry: bool = False
    content_enabled: bool = True


@dataclass(slots=True)
class MemoryConfig:
    enabled: bool = True
    # Memory root. Empty -> per-project default under ~/.bareagent/projects/.
    dir: str = ""
    # Max lines of MEMORY.md injected into the system prompt at session start.
    max_index_lines: int = 200
    # Number of lexically-relevant memories recalled and injected each turn
    # (0 = disable recall, keeping only the session-start index injection).
    recall_k: int = 5


@dataclass(slots=True)
class CostConfig:
    # Per-model price overrides keyed by model id. Each entry is a
    # ``{"input": <usd-per-million>, "output": <usd-per-million>}`` dict that
    # overrides/extends the built-in Claude default prices in token_tracker.py.
    prices: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(slots=True)
class RetryConfig:
    # Mirrors src/core/retry.py:RetryPolicy (same field names + defaults). The
    # app layer owns LLM retries exclusively (SDK clients use max_retries=0).
    enabled: bool = True
    max_attempts: int = 3  # total attempts (incl. first), <=1 disables retries
    base_delay_sec: float = 1.0
    max_delay_sec: float = 30.0
    multiplier: float = 2.0
    jitter: bool = True


@dataclass(slots=True)
class SkillsConfig:
    # Experiential skill generation (task 06-01-experiential-skill-gen): after a
    # complex multi-turn task the agent auto-drafts a reusable skill into a
    # pending area for the user to promote with /skill keep.
    auto_generate: bool = True
    # Double-AND trigger thresholds (cumulative since session start / last draft).
    min_tool_calls: int = 5
    min_user_replies: int = 3
    # Soft cap on pending drafts (oldest pruned beyond this; <=0 disables).
    max_pending: int = 10
    # Generated-skills root override. Empty -> per-project default under
    # ~/.bareagent/projects/<slug>/skills/.
    dir: str = ""


@dataclass(slots=True)
class GoalConfig:
    # Goal completion loop (task 06-06-goal-completion-loop): /goal <condition>
    # drives turns until an isolated evaluator judges the condition met.
    # Turn-budget safety valve; an inline `--max-turns N` overrides per invocation.
    max_turns: int = DEFAULT_MAX_TURNS
    # Optional cheaper model for the per-turn evaluator. Empty -> reuse the
    # session provider/model (no extra client, works for any provider).
    evaluator_model: str = ""


@dataclass(slots=True)
class WorkflowConfig:
    # Deterministic workflow orchestration (task
    # 06-06-workflow-deterministic-orchestration): the LLM authors a static DAG of
    # subagent nodes via the main-loop-only ``workflow`` tool; independent nodes
    # run concurrently. ``enabled=false`` short-circuits the whole feature (the
    # tool is never installed). Honors ``BAREAGENT_WORKFLOW_ENABLED``.
    enabled: bool = True
    # Max nodes that may run concurrently (each node is a full subagent).
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    # Ceiling on declared nodes per workflow; guards the thread pool against an
    # oversized DAG.
    max_nodes: int = DEFAULT_MAX_NODES


@dataclass(slots=True)
class TeamConfig:
    # Multi-agent teammate coordination (task 06-06-team-subsystem-completion).
    # ``poll_interval`` is how long an idle teammate daemon waits between
    # task-scan wakeups (it also wakes immediately on incoming mail via the
    # mailbox condition variable). ``response_timeout`` is how long a blocking
    # ``team_send`` waits for a teammate's reply before returning a timeout note.
    # ``memory_enabled`` (task 06-08-team-stateful-memory) makes a teammate carry
    # conversational memory across *requests* (a per-teammate Compactor is injected
    # to bound growth); off restores the old per-request stateless behavior.
    # All three are baked into spawned teammates / send calls at boot ->
    # restart-required.
    poll_interval: float = 1.0
    response_timeout: float = 60.0
    memory_enabled: bool = True


@dataclass(slots=True)
class Config:
    provider: ProviderConfig
    permission: PermissionConfig
    ui: UIConfig
    subagent: SubagentConfig
    thinking: ThinkingConfig
    debug: DebugConfig
    tracing: TracingConfig
    path: Path
    mcp: MCPConfig
    lsp: LSPConfig
    # Defaulted so existing Config(...) constructions (tests, fixtures) keep
    # working without passing memory explicitly.
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    # Prompt caching (Anthropic explicit cache_control breakpoints). Defined in
    # provider.base so the factory/provider share one type, mirroring thinking.
    cache: CacheConfig = field(default_factory=CacheConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    goal: GoalConfig = field(default_factory=GoalConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    team: TeamConfig = field(default_factory=TeamConfig)


# Dotted config paths that ``/reload`` can hot-apply to live runtime objects.
# Anything else that changes on disk is reported as "requires restart" because
# it was baked into a manager/client/provider at boot (see CLAUDE.md ROADMAP 4.3).
_HOT_RELOAD_PATHS = frozenset(
    {
        "ui.theme",
        "permission.mode",
        "permission.allow",
        "permission.deny",
    }
)


@dataclass(slots=True)
class ConfigChange:
    """A single changed config leaf, identified by its dotted path."""

    path: str  # dotted, e.g. "ui.theme"
    old: Any
    new: Any


@dataclass(slots=True)
class ReloadReport:
    """Classification of a config diff into hot (applied) vs restart-required."""

    hot: list[ConfigChange]  # hot-reloadable and will be applied
    restart: list[ConfigChange]  # changed but only reported (needs restart)

    @property
    def changed(self) -> bool:
        return bool(self.hot or self.restart)


def _flatten_config(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a top-level ``asdict(Config)`` mapping into dotted-path leaves.

    Each top-level Config field (provider/permission/ui/...) is a nested
    dataclass that ``asdict`` rendered as a dict, so we descend exactly one
    level to produce ``section.field`` leaves. Whatever sits at the second level
    (scalar, ``list`` like ``permission.allow``, or ``dict`` like ``cost.prices``)
    is a single leaf compared wholesale — order changes in a list count as a
    change. ``path`` is a scalar and stays a top-level leaf.
    """
    leaves: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                leaves[f"{key}.{sub_key}"] = sub_value
        else:
            leaves[key] = value
    return leaves


def _diff_config_for_reload(old: Config, new: Config) -> ReloadReport:
    """Diff two configs and classify each changed leaf as hot vs restart.

    Pure function (no side effects) so it can be unit tested. The ``path`` field
    (a resolved filesystem path, not a config knob) is skipped entirely.
    """
    old_flat = _flatten_config(asdict(old))
    new_flat = _flatten_config(asdict(new))

    hot: list[ConfigChange] = []
    restart: list[ConfigChange] = []
    for dotted in sorted(set(old_flat) | set(new_flat)):
        if dotted == "path":
            continue
        old_value = old_flat.get(dotted)
        new_value = new_flat.get(dotted)
        if old_value == new_value:
            continue
        change = ConfigChange(path=dotted, old=old_value, new=new_value)
        if dotted in _HOT_RELOAD_PATHS:
            hot.append(change)
        else:
            restart.append(change)
    return ReloadReport(hot=hot, restart=restart)


def _config_mtimes(config: Config) -> dict[str, float]:
    """Best-effort mtimes of config.toml + its .local sibling.

    Missing files are skipped (so creating/deleting the local override is itself
    a detectable change). Used by the passive on-prompt change detector.
    """
    main_path = config.path
    local_path = main_path.with_suffix("").with_name(
        main_path.stem + ".local" + main_path.suffix,
    )
    mtimes: dict[str, float] = {}
    for path in (main_path, local_path):
        try:
            mtimes[str(path)] = os.stat(path).st_mtime
        except OSError:
            continue
    return mtimes


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", help="Override the configured provider name.")
    parser.add_argument("--model", help="Override the configured model name.")
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Path to the TOML config file. Defaults to BAREAGENT_CONFIG or the bundled config.toml."
        ),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bareagent")
    # Top-level flags stay usable with no subcommand so the existing
    # ``bareagent --provider ... --model ...`` REPL invocation is unchanged.
    _add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser(
        "init",
        help="Interactively configure a provider and write config.local.toml.",
    )
    # Allow ``bareagent init --config <path>`` to target a specific config file.
    _add_common_arguments(init_parser)
    return parser.parse_args(argv)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (returns a new dict)."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_config_file(config_path: Path) -> dict:
    with config_path.open("rb") as file:
        base = tomllib.load(file)
    local_path = config_path.with_suffix("").with_name(
        config_path.stem + ".local" + config_path.suffix,
    )
    if local_path.is_file():
        with local_path.open("rb") as file:
            local = tomllib.load(file)
        return _deep_merge(base, local)
    return base


def _resolve_string(
    file_value: str,
    env_name: str,
    cli_value: str | None = None,
) -> str:
    if cli_value is not None:
        return cli_value
    return os.getenv(env_name, file_value)


def _resolve_bool(file_value: bool, env_name: str) -> bool:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return file_value

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} must be a boolean value, got: {raw_value}")


def _resolve_int(file_value: int, env_name: str) -> int:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return file_value
    return int(raw_value)


def _resolve_optional_string(file_value: str | None, env_name: str) -> str | None:
    raw_value = os.getenv(env_name)
    value = raw_value if raw_value is not None else file_value
    if value in {None, ""}:
        return None
    return value


def _validate_mode(name: str, value: str, allowed: AbstractSet[str]) -> str:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_values}")
    return value


def _default_api_key_env(provider_name: str) -> str:
    return DEFAULT_API_KEY_ENV_BY_PROVIDER.get(provider_name.lower(), "ANTHROPIC_API_KEY")


def resolve_config_path(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.expanduser()

    env_path = os.getenv("BAREAGENT_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    return DEFAULT_CONFIG_PATH


def _has_usable_key(provider: ProviderConfig) -> bool:
    """Return whether *provider* can resolve an API key without the wizard.

    Mirrors :func:`src.provider.factory._resolve_api_key`: an explicit
    plaintext ``api_key`` wins, an ``sk-`` prefixed ``api_key_env`` is itself
    the key, and otherwise the named environment variable must be populated.
    """
    if provider.api_key:
        return True
    api_key_env = provider.api_key_env or ""
    if api_key_env.startswith("sk-"):
        return True
    return bool(api_key_env and os.getenv(api_key_env))


def _parse_cost_config(cost_raw: dict) -> CostConfig:
    """Parse the ``[cost]`` / ``[cost.prices]`` config section.

    Each ``[cost.prices."<model-id>"]`` table is coerced into a
    ``{"input": float, "output": float}`` dict (USD per million tokens).
    Malformed or incomplete entries are skipped so a bad override never crashes
    boot — the model simply shows token counts without a ``$`` estimate.
    """
    prices_raw = cost_raw.get("prices", {})
    prices: dict[str, dict[str, float]] = {}
    if isinstance(prices_raw, dict):
        for model, entry in prices_raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                prices[str(model)] = {
                    "input": float(entry["input"]),
                    "output": float(entry["output"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return CostConfig(prices=prices)


def _parse_retry_config(retry_raw: dict) -> RetryConfig:
    """Parse the ``[retry]`` config section.

    Each field is parsed defensively — a malformed value falls back to the
    default rather than crashing boot (mirrors ``_parse_cost_config``).
    ``enabled`` / ``max_attempts`` honor env overrides
    (``BAREAGENT_RETRY_ENABLED`` / ``BAREAGENT_RETRY_MAX_ATTEMPTS``); the
    remaining fields are config-only.
    """
    defaults = RetryConfig()
    try:
        enabled = _resolve_bool(
            bool(retry_raw.get("enabled", defaults.enabled)),
            "BAREAGENT_RETRY_ENABLED",
        )
    except (TypeError, ValueError):
        enabled = defaults.enabled
    try:
        max_attempts = _resolve_int(
            int(retry_raw.get("max_attempts", defaults.max_attempts)),
            "BAREAGENT_RETRY_MAX_ATTEMPTS",
        )
    except (TypeError, ValueError):
        max_attempts = defaults.max_attempts
    try:
        base_delay_sec = float(retry_raw.get("base_delay_sec", defaults.base_delay_sec))
    except (TypeError, ValueError):
        base_delay_sec = defaults.base_delay_sec
    try:
        max_delay_sec = float(retry_raw.get("max_delay_sec", defaults.max_delay_sec))
    except (TypeError, ValueError):
        max_delay_sec = defaults.max_delay_sec
    try:
        multiplier = float(retry_raw.get("multiplier", defaults.multiplier))
    except (TypeError, ValueError):
        multiplier = defaults.multiplier
    try:
        jitter = bool(retry_raw.get("jitter", defaults.jitter))
    except (TypeError, ValueError):
        jitter = defaults.jitter
    return RetryConfig(
        enabled=enabled,
        max_attempts=max_attempts,
        base_delay_sec=base_delay_sec,
        max_delay_sec=max_delay_sec,
        multiplier=multiplier,
        jitter=jitter,
    )


def _build_retry_policy(retry_config: RetryConfig) -> RetryPolicy:
    return RetryPolicy(
        enabled=retry_config.enabled,
        max_attempts=retry_config.max_attempts,
        base_delay_sec=retry_config.base_delay_sec,
        max_delay_sec=retry_config.max_delay_sec,
        multiplier=retry_config.multiplier,
        jitter=retry_config.jitter,
    )


def _parse_cache_config(cache_raw: dict) -> CacheConfig:
    """Parse the ``[cache]`` config section (defensive, never crashes boot).

    ``enabled`` honors the ``BAREAGENT_CACHE_ENABLED`` env override (mirrors
    ``[retry]``); ``ttl`` is config-only and falls back to ``"5m"`` for any
    value outside ``{"5m", "1h"}``. Only the Anthropic provider acts on this.
    """
    defaults = CacheConfig()
    try:
        enabled = _resolve_bool(
            bool(cache_raw.get("enabled", defaults.enabled)),
            "BAREAGENT_CACHE_ENABLED",
        )
    except (TypeError, ValueError):
        enabled = defaults.enabled
    ttl_raw = str(cache_raw.get("ttl", defaults.ttl)).strip().lower()
    ttl = cast(Literal["5m", "1h"], ttl_raw) if ttl_raw in VALID_CACHE_TTLS else defaults.ttl
    return CacheConfig(enabled=enabled, ttl=ttl)


def _parse_skills_config(skills_raw: dict) -> SkillsConfig:
    """Parse the ``[skills]`` config section (defensive, never crashes boot).

    ``auto_generate`` honors ``BAREAGENT_SKILLS_AUTO_GENERATE`` (mirrors
    ``[retry]``/``[cache]``); the rest are config-only and fall back per field.
    Note ``BAREAGENT_SKILLS_DIR`` is a *separate* knob for the repo canon dir
    (``resolve_skills_dir``), so the generated-root override here is config-only.
    """
    defaults = SkillsConfig()
    try:
        auto_generate = _resolve_bool(
            bool(skills_raw.get("auto_generate", defaults.auto_generate)),
            "BAREAGENT_SKILLS_AUTO_GENERATE",
        )
    except (TypeError, ValueError):
        auto_generate = defaults.auto_generate

    def _int_field(key: str, fallback: int) -> int:
        try:
            return int(skills_raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return SkillsConfig(
        auto_generate=auto_generate,
        min_tool_calls=_int_field("min_tool_calls", defaults.min_tool_calls),
        min_user_replies=_int_field("min_user_replies", defaults.min_user_replies),
        max_pending=_int_field("max_pending", defaults.max_pending),
        dir=str(skills_raw.get("dir", defaults.dir)),
    )


def _build_skillgen_config(skills: SkillsConfig) -> SkillGenConfig:
    """Adapt the user-facing ``SkillsConfig`` to the pure ``SkillGenConfig``."""
    return SkillGenConfig(
        enabled=skills.auto_generate,
        min_tool_calls=skills.min_tool_calls,
        min_user_replies=skills.min_user_replies,
    )


def _parse_goal_config(goal_raw: dict) -> GoalConfig:
    """Parse the ``[goal]`` config section (defensive, never crashes boot).

    ``max_turns`` honors ``BAREAGENT_GOAL_MAX_TURNS`` (mirrors ``[retry]``);
    ``evaluator_model`` is config-only. A malformed value falls back to the
    default per field.
    """
    defaults = GoalConfig()
    try:
        max_turns = _resolve_int(
            int(goal_raw.get("max_turns", defaults.max_turns)),
            "BAREAGENT_GOAL_MAX_TURNS",
        )
    except (TypeError, ValueError):
        max_turns = defaults.max_turns
    if max_turns < 1:
        max_turns = defaults.max_turns
    return GoalConfig(
        max_turns=max_turns,
        evaluator_model=str(goal_raw.get("evaluator_model", defaults.evaluator_model)).strip(),
    )


def _parse_workflow_config(workflow_raw: dict) -> WorkflowConfig:
    """Parse the ``[workflow]`` config section (defensive, never crashes boot).

    ``enabled`` honors ``BAREAGENT_WORKFLOW_ENABLED``; the integer caps are
    config-only and fall back to their default when missing / malformed / < 1.
    """
    defaults = WorkflowConfig()
    try:
        enabled = _resolve_bool(
            bool(workflow_raw.get("enabled", defaults.enabled)),
            "BAREAGENT_WORKFLOW_ENABLED",
        )
    except (TypeError, ValueError):
        enabled = defaults.enabled

    def _positive_int(key: str, fallback: int) -> int:
        try:
            value = int(workflow_raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value >= 1 else fallback

    return WorkflowConfig(
        enabled=enabled,
        max_concurrency=_positive_int("max_concurrency", defaults.max_concurrency),
        max_nodes=_positive_int("max_nodes", defaults.max_nodes),
    )


def _parse_team_config(team_raw: dict) -> TeamConfig:
    """Parse the ``[team]`` config section (defensive, never crashes boot).

    ``poll_interval`` / ``response_timeout`` are config-only positive floats; a
    missing / malformed / <= 0 value falls back to its default. ``memory_enabled``
    honors the ``BAREAGENT_TEAM_MEMORY_ENABLED`` env override (mirrors retry /
    cache / workflow ``enabled`` knobs).
    """
    defaults = TeamConfig()

    def _positive_float(key: str, fallback: float) -> float:
        try:
            value = float(team_raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    try:
        memory_enabled = _resolve_bool(
            bool(team_raw.get("memory_enabled", defaults.memory_enabled)),
            "BAREAGENT_TEAM_MEMORY_ENABLED",
        )
    except (TypeError, ValueError):
        memory_enabled = defaults.memory_enabled

    return TeamConfig(
        poll_interval=_positive_float("poll_interval", defaults.poll_interval),
        response_timeout=_positive_float("response_timeout", defaults.response_timeout),
        memory_enabled=memory_enabled,
    )


def _build_goal_provider(
    config: Config,
    session_provider: BaseLLMProvider,
) -> BaseLLMProvider:
    """Provider for the goal evaluator: a cheaper model if configured, else reuse.

    ``[goal] evaluator_model`` empty -> reuse the session provider (no extra
    client). Otherwise build a sibling provider with that model via the factory
    (same provider family / credentials). On any build failure, warn and fall
    back to the session provider so a bad model id never blocks ``/goal``.
    """
    model = config.goal.evaluator_model.strip()
    if not model:
        return session_provider
    try:
        eval_config = replace(config, provider=replace(config.provider, model=model))
        return create_provider(eval_config)
    except Exception as exc:  # noqa: BLE001 - never block /goal on evaluator setup
        _log.warning(
            "Goal evaluator provider build failed (%s); reusing session provider.",
            exc,
        )
        return session_provider


def load_config(
    config_path: Path,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> Config:
    raw_config = _read_config_file(config_path)
    provider_raw = raw_config.get("provider", {})
    permission_raw = raw_config.get("permission", {})
    ui_raw = raw_config.get("ui", {})
    subagent_raw = raw_config.get("subagent", {})
    thinking_raw = raw_config.get("thinking", {})
    debug_raw = raw_config.get("debug", {})
    tracing_raw = raw_config.get("tracing", {})
    allow_rules, deny_rules = parse_permission_rules(raw_config)
    configured_provider_name = str(provider_raw.get("name", "anthropic"))
    provider_name = _resolve_string(
        configured_provider_name,
        "BAREAGENT_PROVIDER",
        provider_override,
    )
    default_api_key_env = _default_api_key_env(provider_name)
    configured_api_key_env = provider_raw.get("api_key_env")
    api_key_env_default = (
        configured_api_key_env
        if configured_api_key_env and provider_name == configured_provider_name
        else default_api_key_env
    )

    provider = ProviderConfig(
        name=provider_name,
        model=_resolve_string(
            provider_raw.get("model", "claude-sonnet-4-20250514"),
            "BAREAGENT_MODEL",
            model_override,
        ),
        api_key_env=_resolve_string(
            api_key_env_default,
            "BAREAGENT_API_KEY_ENV",
        ),
        api_key=_resolve_optional_string(
            provider_raw.get("api_key"),
            "BAREAGENT_API_KEY",
        ),
        base_url=_resolve_optional_string(
            provider_raw.get("base_url"),
            "BAREAGENT_BASE_URL",
        ),
        wire_api=_resolve_optional_string(
            provider_raw.get("wire_api"),
            "BAREAGENT_WIRE_API",
        ),
    )
    permission = PermissionConfig(
        mode=_validate_mode(
            "permission.mode",
            _resolve_string(
                permission_raw.get("mode", "default"),
                "BAREAGENT_PERMISSION_MODE",
            ),
            VALID_PERMISSION_MODES,
        ),
        allow=allow_rules,
        deny=deny_rules,
    )
    ui = UIConfig(
        stream=_resolve_bool(ui_raw.get("stream", True), "BAREAGENT_UI_STREAM"),
        theme=_resolve_string(ui_raw.get("theme", "dark"), "BAREAGENT_UI_THEME"),
    )
    try:
        subagent_max_resumable = int(subagent_raw.get("max_resumable", 20))
    except (TypeError, ValueError):
        subagent_max_resumable = 20
    if subagent_max_resumable < 1:
        subagent_max_resumable = 20
    subagent = SubagentConfig(
        max_depth=_resolve_int(
            int(subagent_raw.get("max_depth", 3)),
            "BAREAGENT_SUBAGENT_MAX_DEPTH",
        ),
        default_type=_validate_mode(
            "subagent.default_type",
            _resolve_string(
                str(subagent_raw.get("default_type", DEFAULT_AGENT_TYPE)),
                "BAREAGENT_SUBAGENT_DEFAULT_TYPE",
            ),
            VALID_SUBAGENT_TYPES,
        ),
        max_resumable=subagent_max_resumable,
    )
    thinking = ThinkingConfig(
        mode=cast(
            Literal["enabled", "adaptive", "disabled"],
            _validate_mode(
                "thinking.mode",
                _resolve_string(
                    thinking_raw.get("mode", "adaptive"),
                    "BAREAGENT_THINKING_MODE",
                ),
                VALID_THINKING_MODES,
            ),
        ),
        budget_tokens=_resolve_int(
            int(thinking_raw.get("budget_tokens", 10000)),
            "BAREAGENT_THINKING_BUDGET_TOKENS",
        ),
    )
    debug = DebugConfig(
        enabled=_resolve_bool(
            bool(debug_raw.get("enabled", False)),
            "BAREAGENT_DEBUG",
        ),
        log_dir=_resolve_string(
            str(debug_raw.get("log_dir", ".logs")),
            "BAREAGENT_DEBUG_LOG_DIR",
        ),
        viewer_port=_resolve_int(
            int(debug_raw.get("viewer_port", 8321)),
            "BAREAGENT_DEBUG_VIEWER_PORT",
        ),
        pretty=_resolve_bool(
            bool(debug_raw.get("pretty", True)),
            "BAREAGENT_DEBUG_PRETTY",
        ),
    )
    tracing = TracingConfig(
        langfuse=_resolve_bool(
            bool(tracing_raw.get("langfuse", False)),
            "BAREAGENT_TRACING_LANGFUSE",
        ),
        opentelemetry=_resolve_bool(
            bool(tracing_raw.get("opentelemetry", False)),
            "BAREAGENT_TRACING_OPENTELEMETRY",
        ),
        content_enabled=_resolve_bool(
            bool(tracing_raw.get("content_enabled", True)),
            "BAREAGENT_CONTENT_TRACING_ENABLED",
        ),
    )

    mcp_raw = raw_config.get("mcp", {})
    try:
        mcp_config = parse_mcp_config({"mcp": mcp_raw} if isinstance(mcp_raw, dict) else {})
    except MCPError as exc:
        print(f"Warning: invalid [mcp] config, MCP disabled ({exc})")
        mcp_config = MCPConfig()

    lsp_raw = raw_config.get("lsp", {})
    try:
        lsp_config = parse_lsp_config({"lsp": lsp_raw} if isinstance(lsp_raw, dict) else {})
    except LSPError as exc:
        print(f"Warning: invalid [lsp] config, LSP disabled ({exc})")
        lsp_config = LSPConfig()

    hooks_raw = raw_config.get("hooks", [])
    try:
        hooks_config = parse_hooks_config(
            {"hooks": hooks_raw} if isinstance(hooks_raw, list) else {}
        )
    except HookConfigError as exc:
        print(f"Warning: invalid [[hooks]] config, hooks disabled ({exc})")
        hooks_config = HooksConfig()
    for skipped_reason in hooks_config.skipped:
        print(f"Warning: {skipped_reason}")

    cost_raw = raw_config.get("cost", {})
    cost_config = _parse_cost_config(cost_raw if isinstance(cost_raw, dict) else {})

    retry_raw = raw_config.get("retry", {})
    retry_config = _parse_retry_config(retry_raw if isinstance(retry_raw, dict) else {})

    cache_raw = raw_config.get("cache", {})
    cache_config = _parse_cache_config(cache_raw if isinstance(cache_raw, dict) else {})

    skills_raw = raw_config.get("skills", {})
    skills_config = _parse_skills_config(skills_raw if isinstance(skills_raw, dict) else {})

    goal_raw = raw_config.get("goal", {})
    goal_config = _parse_goal_config(goal_raw if isinstance(goal_raw, dict) else {})

    workflow_raw = raw_config.get("workflow", {})
    workflow_config = _parse_workflow_config(workflow_raw if isinstance(workflow_raw, dict) else {})

    team_raw = raw_config.get("team", {})
    team_config = _parse_team_config(team_raw if isinstance(team_raw, dict) else {})

    memory_raw = raw_config.get("memory", {})
    memory_config = MemoryConfig(
        enabled=_resolve_bool(
            bool(memory_raw.get("enabled", True)),
            "BAREAGENT_MEMORY_ENABLED",
        ),
        dir=_resolve_string(
            str(memory_raw.get("dir", "")),
            "BAREAGENT_MEMORY_DIR",
        ),
        max_index_lines=_resolve_int(
            int(memory_raw.get("max_index_lines", 200)),
            "BAREAGENT_MEMORY_MAX_INDEX_LINES",
        ),
        recall_k=_resolve_int(
            int(memory_raw.get("recall_k", 5)),
            "BAREAGENT_MEMORY_RECALL_K",
        ),
    )

    return Config(
        provider=provider,
        permission=permission,
        ui=ui,
        subagent=subagent,
        thinking=thinking,
        debug=debug,
        tracing=tracing,
        path=config_path.resolve(),
        mcp=mcp_config,
        lsp=lsp_config,
        memory=memory_config,
        cost=cost_config,
        hooks=hooks_config,
        retry=retry_config,
        cache=cache_config,
        skills=skills_config,
        goal=goal_config,
        workflow=workflow_config,
        team=team_config,
    )


_NAG_REMINDER_PREFIX = "<nag-reminder>"
_MEMORY_RECALL_PREFIX = "<memory-recall>"
_PLAN_DIRECTIVE_PREFIX = "<plan-mode>"


def _initial_messages(
    workspace: Path,
    skill_summary: str = "",
    memory_context: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": assemble_system_prompt(
                workspace,
                skill_summary=skill_summary,
                memory_context=memory_context,
            ),
        }
    ]


def _build_memory_manager(
    config: Config,
    workspace_path: Path,
    ui_console: AgentConsole,
) -> MemoryManager | None:
    """Build the persistent memory manager, or None when disabled/unavailable."""
    if not config.memory.enabled:
        return None
    try:
        root = resolve_memory_root(workspace_path, config.memory.dir)
        return MemoryManager(root, max_index_lines=config.memory.max_index_lines)
    except OSError as exc:
        ui_console.print_error(f"Persistent memory disabled (cannot open store): {exc}")
        return None


def _memory_context(memory_manager: MemoryManager | None) -> str:
    return memory_manager.system_prompt_section() if memory_manager is not None else ""


def _refresh_nag_reminder(
    messages: list[dict[str, Any]],
    nag_reminder: str | None,
) -> None:
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and str(message["content"]).startswith(_NAG_REMINDER_PREFIX)
        )
    ]
    if not nag_reminder:
        return

    nag_message = {
        "role": "system",
        "content": f"{_NAG_REMINDER_PREFIX}\n{nag_reminder.strip()}\n</nag-reminder>",
    }
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and not is_tool_result_message(msg):
            messages.insert(index + 1, nag_message)
            return

    messages.append(nag_message)


def _refresh_memory_recall(
    messages: list[dict[str, Any]],
    memory_manager: MemoryManager | None,
    recall_k: int,
) -> None:
    """Drop the stale recall block and inject one for the latest user turn.

    Mirrors :func:`_refresh_nag_reminder`: a single ``<memory-recall>`` system
    message lives just after the most recent genuine user message, refreshed on
    every agent-loop iteration so ``/remember``, ``/forget`` and ordinary turns
    all pick up the latest lexically-relevant memories.
    """
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and str(message["content"]).startswith(_MEMORY_RECALL_PREFIX)
        )
    ]
    if memory_manager is None or recall_k <= 0:
        return

    query: str | None = None
    insert_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and not is_tool_result_message(msg):
            content = msg.get("content")
            if isinstance(content, str):
                query = content
                insert_index = index
            break
    if query is None or insert_index is None:
        return

    section = memory_manager.recall_section(query, recall_k)
    if not section:
        return

    messages.insert(insert_index + 1, {"role": "system", "content": section})


def _refresh_plan_directive(
    messages: list[dict[str, Any]],
    permission: PermissionGuard,
) -> None:
    """Drop any stale plan-mode directive and re-inject it while in PLAN mode.

    Mirrors :func:`_refresh_nag_reminder`. Because ``compact`` runs at the top
    of every agent-loop iteration (``loop.py``), approving a plan mid-loop flips
    ``permission.mode`` and the *next* iteration strips this block automatically
    -- no stale plan guidance lingers once execution begins.
    """
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and str(message["content"]).startswith(_PLAN_DIRECTIVE_PREFIX)
        )
    ]
    if permission.mode != PermissionMode.PLAN:
        return

    directive = {
        "role": "system",
        "content": f"{_PLAN_DIRECTIVE_PREFIX}\n{PLAN_MODE_DIRECTIVE}\n</plan-mode>",
    }
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and not is_tool_result_message(msg):
            messages.insert(index + 1, directive)
            return

    messages.append(directive)


def _build_loop_compact(
    compact_fn: object,
    todo_manager: TodoManager,
    memory_manager: MemoryManager | None = None,
    recall_k: int = 0,
    permission: PermissionGuard | None = None,
):
    def _compact(
        messages: list[dict[str, Any]],
        force: bool = False,
    ) -> None:
        _refresh_nag_reminder(messages, todo_manager.get_nag_reminder())
        _refresh_memory_recall(messages, memory_manager, recall_k)
        if permission is not None:
            _refresh_plan_directive(messages, permission)
        compact_fn(messages, force=force)  # type: ignore[misc]

    get_session_id = getattr(compact_fn, "get_session_id", None)
    if callable(get_session_id):
        _compact.get_session_id = get_session_id  # type: ignore[attr-defined]

    set_session_id = getattr(compact_fn, "set_session_id", None)
    if callable(set_session_id):
        _compact.set_session_id = set_session_id  # type: ignore[attr-defined]

    return _compact


_PERMISSION_SLASH = {
    "/default": PermissionMode.DEFAULT,
    "/auto": PermissionMode.AUTO,
    "/plan": PermissionMode.PLAN,
    "/bypass": PermissionMode.BYPASS,
}
_MODE_CYCLE = [
    PermissionMode.DEFAULT,
    PermissionMode.AUTO,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]
_MODE_DESCRIPTIONS = {
    PermissionMode.DEFAULT: "Write operations require confirmation",
    PermissionMode.AUTO: "Safe commands auto-approved",
    PermissionMode.PLAN: "Read-only mode",
    PermissionMode.BYPASS: "No confirmation required",
}
_SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/clear",
    "/new",
    "/compact",
    *_PERMISSION_SLASH,
    "/mode",
    "/theme",
    "/sessions",
    "/resume",
    "/export",
    "/import",
    "/cost",
    "/goal",
    "/loop",
    "/log",
    "/team",
    "/mcp",
    "/mcp:",
    "/lsp",
    "/reload",
    "/remember",
    "/forget",
    "/skill",
]
_HELP_TEXT = (
    "Available commands:\n"
    "  /help      Show this help message\n"
    "  /exit      Exit BareAgent\n"
    "  /clear     Clear screen and start new conversation\n"
    "  /new       Start a new conversation\n"
    "  /compact   Compress conversation context\n"
    "  /default   Switch to DEFAULT permission mode\n"
    "  /auto      Switch to AUTO permission mode\n"
    "  /plan      Switch to PLAN permission mode\n"
    "  /bypass    Switch to BYPASS permission mode\n"
    "  /mode      Interactive permission mode selection\n"
    "  /theme     Switch color theme (catppuccin-mocha, dracula, nord, tokyo-night, gruvbox)\n"
    "  /sessions  List saved sessions\n"
    "  /resume    Resume a previous session\n"
    "  /export    Export conversation (markdown default | json) [path]\n"
    "  /import    Import a conversation file (.json/.jsonl) into a new session\n"
    "  /cost      Show token usage and estimated cost for this session\n"
    "  /goal      Drive the agent until a condition is met "
    "(/goal [--max-turns N] <condition>); respects current permission mode\n"
    "  /loop      Schedule a shell command to repeat every N seconds "
    "(list|cancel <id>|clear); runs WITHOUT permission prompts\n"
    "  /log       Debug log viewer (status|serve|open|<seq>)\n"
    "  /team      Manage team agents (list | spawn | send | shutdown | register | review)\n"
    "  /mcp       Manage MCP servers (status | list | reload <name>)\n"
    "  /mcp:      Invoke an MCP prompt (e.g. /mcp:server:prompt key=value)\n"
    "  /lsp       Manage LSP servers (status | list | reload <language>)\n"
    "  /reload    Reload config.toml (theme + permission hot-apply; others need restart)\n"
    "  /remember  Save information to persistent memory (/remember <text>)\n"
    "  /forget    Remove information from persistent memory (/forget <text>)\n"
    "  /skill     Manage generated skills (list | keep <name> | discard <name>)"
)


def _build_permission_guard(config: Config) -> PermissionGuard:
    guard = PermissionGuard(PermissionMode(config.permission.mode))
    guard.allow_rules = list(config.permission.allow)
    guard.deny_rules = list(config.permission.deny)
    return guard


def _format_config_change(change: ConfigChange) -> str:
    return f"{change.path} {change.old!r}→{change.new!r}"


def _dispatch_reload_command(
    *,
    config: Config,
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    """Re-read config from disk and hot-apply the theme + permission subset.

    All-or-nothing failure safety: if ``load_config`` raises (bad TOML, validation
    failure) the current live config is left untouched. Hot-reloadable changes
    (``ui.theme`` + ``permission.{mode,allow,deny}``) are applied to the live
    runtime objects *and* mirrored back into ``config`` so later reads stay
    consistent; everything else is only reported as "requires restart".
    """
    from src.ui.theme import format_unknown_theme, get_theme

    try:
        new_config = load_config(config.path)
    except Exception as exc:
        ui_console.print_error(
            f"Reload failed ({type(exc).__name__}: {exc}). Keeping current config."
        )
        return

    report = _diff_config_for_reload(config, new_config)
    if not report.changed:
        ui_console.print_status("Config unchanged.")
        return

    applied: list[str] = []
    for change in report.hot:
        if change.path == "ui.theme":
            tm = get_theme()
            if tm.switch(new_config.ui.theme):
                ui_console.set_theme(tm)
                config.ui.theme = new_config.ui.theme
                applied.append(_format_config_change(change))
            else:
                ui_console.print_error(format_unknown_theme(new_config.ui.theme))
        elif change.path == "permission.mode":
            try:
                permission.mode = PermissionMode(new_config.permission.mode)
            except ValueError:
                ui_console.print_error(
                    f"Invalid permission.mode {new_config.permission.mode!r}; skipped."
                )
                continue
            config.permission.mode = new_config.permission.mode
            applied.append(_format_config_change(change))
        elif change.path == "permission.allow":
            permission.allow_rules = list(new_config.permission.allow)
            config.permission.allow = list(new_config.permission.allow)
            applied.append(_format_config_change(change))
        elif change.path == "permission.deny":
            permission.deny_rules = list(new_config.permission.deny)
            config.permission.deny = list(new_config.permission.deny)
            applied.append(_format_config_change(change))

    if applied:
        ui_console.print_status("Reloaded: " + ", ".join(applied))
    if report.restart:
        restart_summary = ", ".join(_format_config_change(change) for change in report.restart)
        ui_console.print_status(
            f"Changed but requires restart: {restart_summary} (restart BareAgent to apply)"
        )


def _build_permission_allow_rule(
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    normalized_tool = tool_name.strip().lower()
    subject = permission_rule_subject(normalized_tool, tool_input)
    if not subject:
        return None
    if "\n" in subject or "\r" in subject:
        encoded_subject = json.dumps(subject, ensure_ascii=False)
        return f"{normalized_tool}(prefix_json:{encoded_subject})"
    return f"{normalized_tool}(prefix:{subject}*)"


def _persist_permission_allow_rule(
    permission: PermissionGuard,
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    rule = _build_permission_allow_rule(tool_name, tool_input)
    if rule is None or rule in permission.allow_rules:
        return
    permission.allow_rules.append(rule)


def _install_stdio_permission_prompt(
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    if not sys.stdin.isatty():
        return

    def _ask(call: Any) -> bool:
        preview_input = _build_permission_ask_payload(permission, call.name, call.input)
        allowed = ui_console.ask_permission(call.name, preview_input)
        choice = ui_console.consume_permission_choice()
        if allowed and choice == "always":
            _persist_permission_allow_rule(permission, call.name, call.input)
        return allowed

    permission._ask_user_fn = _ask


def _build_permission_ask_payload(
    permission: PermissionGuard,
    tool_name: str,
    tool_input: Any,
) -> Any:
    """Truncate oversized top-level string fields when asking about an MCP tool.

    Non-MCP tools fall back to the raw input dict so existing rendering and
    permission rules stay unchanged. For MCP tools the guard's
    ``format_preview`` rule (256 chars per top-level string) is applied by
    rebuilding the dict — the console layer keeps doing the JSON pretty-print.
    """
    if not isinstance(tool_input, dict):
        return tool_input
    normalized = tool_name.strip().lower()
    if not normalized.startswith("mcp__"):
        return tool_input
    # Reuse the guard's truncation rule by parsing the formatted JSON back into
    # a dict — keeps the truncation logic in one place.
    try:
        truncated = json.loads(permission.format_preview(tool_name, tool_input))
    except (TypeError, ValueError):
        return tool_input
    if not isinstance(truncated, dict):
        return tool_input
    return truncated


def _generate_session_id(
    transcript_mgr: TranscriptManager,
    *,
    reserved_ids: set[str] | None = None,
) -> str:
    known_session_ids = set(transcript_mgr.list_sessions())
    if reserved_ids:
        known_session_ids.update(session_id for session_id in reserved_ids if session_id)

    while True:
        candidate = (
            f"{datetime.now().strftime(_SESSION_ID_TIMESTAMP_FORMAT)}-{generate_random_id(6)}"
        )
        if candidate not in known_session_ids:
            return candidate


def _switch_session_mailbox(
    workspace_path: Path,
    session_id: str,
    *,
    current_bus: MessageBus | None = None,
) -> tuple[MessageBus, str | None]:
    if current_bus is not None:
        _broadcast_team_shutdown(current_bus)

    message_bus = MessageBus(workspace_path / ".mailbox" / session_id)
    message_bus.ensure_mailbox(MAIN_AGENT_NAME)
    return message_bus, message_bus.latest_message_id(MAIN_AGENT_NAME)


def _get_compact_session_id(compact_fn: object) -> str:
    getter = getattr(compact_fn, "get_session_id", None)
    if callable(getter):
        return str(getter())
    return "default"


def _set_compact_session_id(compact_fn: object, session_id: str) -> None:
    setter = getattr(compact_fn, "set_session_id", None)
    if callable(setter):
        setter(session_id)


def _save_transcript_snapshot(
    transcript_mgr: TranscriptManager,
    messages: list[dict[str, Any]],
    compact_fn: object,
) -> None:
    transcript_mgr.save(messages, _get_compact_session_id(compact_fn))


def _resolve_debug_log_dir(workspace_path: Path, config: Config) -> Path:
    log_dir = Path(config.debug.log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = workspace_path / log_dir
    return log_dir


def _build_interaction_logger(
    config: Config,
    workspace_path: Path,
    session_id: str,
) -> InteractionLogger | None:
    if not config.debug.enabled:
        return None

    return InteractionLogger(
        log_dir=_resolve_debug_log_dir(workspace_path, config),
        session_id=session_id,
        pretty=config.debug.pretty,
    )


def _set_interaction_logger_session(
    interaction_logger: InteractionLogger | None,
    session_id: str,
) -> None:
    if interaction_logger is None:
        return
    interaction_logger.session_id = session_id


def _configure_tracing(
    config: Config,
    *,
    session_id: str = "default",
    interaction_logger: InteractionLogger | None = None,
) -> None:
    from src.tracing.setup import configure_tracing

    configure_tracing(
        config.tracing,
        session_id=session_id,
        interaction_logger=interaction_logger,
    )


def _debug_viewer_url(config: Config) -> str:
    return f"http://127.0.0.1:{config.debug.viewer_port}"


def _format_log_status(
    config: Config,
    interaction_logger: InteractionLogger,
    viewer_server: object | None,
) -> str:
    interactions = interaction_logger.list_interactions(interaction_logger.session_id)
    total_tokens = sum(
        int(interaction.get("input_tokens", 0) or 0) + int(interaction.get("output_tokens", 0) or 0)
        for interaction in interactions
    )
    lines = [
        "Debug logging: enabled",
        f"Log dir: {config.debug.log_dir}",
        f"Current session: {interaction_logger.session_id}",
        f"Interactions: {len(interactions)}",
        f"Total tokens: {total_tokens}",
        f"Sessions: {len(interaction_logger.list_sessions())}",
    ]
    if viewer_server is None:
        lines.append("Viewer: not running (use /log serve)")
    else:
        lines.append(f"Viewer: {_debug_viewer_url(config)}")
    return "\n".join(lines)


def _format_log_interaction_summary(
    seq: int,
    interaction: dict[str, object],
) -> str:
    request = interaction.get("request")
    response = interaction.get("response")
    if not request and not response:
        return f"Interaction #{seq} not found."

    response_data = response if isinstance(response, dict) else {}
    tool_calls = response_data.get("tool_calls", [])
    thinking = str(response_data.get("thinking", "") or "").strip()
    lines = [
        f"Interaction #{seq}:",
        f"  Input tokens:  {response_data.get('input_tokens', '?')}",
        f"  Output tokens: {response_data.get('output_tokens', '?')}",
        f"  Duration:      {response_data.get('duration_ms', '?')}ms",
        f"  Tool calls:    {len(tool_calls) if isinstance(tool_calls, list) else 0}",
    ]
    if response_data.get("error"):
        lines.append(f"  Error: {response_data['error']}")
    if thinking:
        preview = thinking[:100]
        if len(thinking) > 100:
            preview += "..."
        lines.append(f"  Thinking: {preview}")
    return "\n".join(lines)


def _start_debug_viewer(
    interaction_logger: InteractionLogger,
    config: Config,
) -> object:
    from src.debug.web_viewer import start_viewer

    viewer_server, _ = start_viewer(
        interaction_logger,
        port=config.debug.viewer_port,
    )
    return viewer_server


def _handle_log_command(
    text: str,
    *,
    config: Config,
    interaction_logger: InteractionLogger | None,
    viewer_server: object | None,
    print_status: Callable[[str], None],
) -> object | None:
    _, _, log_arg = text.partition(" ")
    log_cmd = log_arg.strip()

    if interaction_logger is None:
        print_status(
            "Debug logging is disabled. Set [debug] enabled = true in config.toml "
            "or BAREAGENT_DEBUG=1"
        )
        return viewer_server

    if not log_cmd or log_cmd == "status":
        print_status(_format_log_status(config, interaction_logger, viewer_server))
        return viewer_server

    if log_cmd in {"serve", "open"}:
        if viewer_server is None:
            try:
                viewer_server = _start_debug_viewer(interaction_logger, config)
            except OSError as exc:
                print_status(f"Failed to start debug viewer: {exc}")
                return viewer_server
            print_status(f"Debug viewer started at {_debug_viewer_url(config)}")
        elif log_cmd == "serve":
            print_status(f"Viewer already running at {_debug_viewer_url(config)}")

        if log_cmd == "open":
            import webbrowser

            url = _debug_viewer_url(config)
            webbrowser.open(url)
            print_status(f"Opening {url} in browser...")
        return viewer_server

    try:
        seq = int(log_cmd)
    except ValueError:
        print_status("Usage: /log [status|serve|open|<seq>]")
        return viewer_server

    interaction = interaction_logger.get_interaction(
        interaction_logger.session_id,
        seq,
    )
    print_status(_format_log_interaction_summary(seq, interaction))
    return viewer_server


def _build_handlers(
    *,
    workspace_path: Path,
    todo_manager: TodoManager,
    task_manager: TaskManager | None,
    skill_loader: SkillLoader,
    provider: BaseLLMProvider,
    tools: list[dict[str, object]],
    permission: PermissionGuard,
    bg_manager: BackgroundManager,
    messages: list[dict[str, Any]],
    config: Config,
    runtime_id: str,
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
    mcp_manager: MCPManager | None = None,
    lsp_manager: LanguageServerManager | None = None,
    memory_manager: MemoryManager | None = None,
    system_prompt_override: str | None = None,
    subagent_registry: SubagentRegistry | None = None,
) -> dict[str, Callable[..., Any]]:
    system_prompt = system_prompt_override or _extract_system_prompt(messages)
    team_handlers = _make_team_handlers(
        config=config,
        workspace_path=workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        permission=permission,
        bg_manager=bg_manager,
        tools=tools,
        runtime_id=runtime_id,
        teammate_manager=teammate_manager,
        message_bus=message_bus,
        spawned_agents=spawned_agents,
        agent_name=agent_name,
    )
    return get_handlers(
        workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        provider=provider,
        tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        subagent_system_prompt=system_prompt,
        subagent_max_depth=config.subagent.max_depth,
        subagent_default_type=config.subagent.default_type,
        team_handlers=team_handlers,
        mcp_manager=mcp_manager,
        lsp_manager=lsp_manager,
        memory_manager=memory_manager,
        subagent_retry_policy=_build_retry_policy(config.retry),
        subagent_registry=subagent_registry,
    )


def _load_task_manager(
    workspace_path: Path,
    agent_console: AgentConsole,
) -> TaskManager | None:
    try:
        return TaskManager(workspace_path / ".tasks.json")
    except Exception as exc:
        agent_console.print_error(
            f"Failed to load task file {workspace_path / '.tasks.json'}: {exc}"
        )
        return None


def _load_teammate_manager(
    workspace_path: Path,
    agent_console: AgentConsole,
) -> TeammateManager:
    team_file = workspace_path / ".team.json"
    try:
        return TeammateManager(team_file)
    except Exception as exc:
        agent_console.print_error(f"Failed to load team file {team_file}: {exc}")
        return TeammateManager.create_empty(team_file)


def _extract_system_prompt(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _make_team_handlers(
    *,
    config: Config,
    workspace_path: Path,
    todo_manager: TodoManager,
    task_manager: TaskManager | None,
    skill_loader: SkillLoader,
    permission: PermissionGuard,
    bg_manager: BackgroundManager,
    tools: list[dict[str, object]],
    runtime_id: str,
    teammate_manager: TeammateManager,
    message_bus: MessageBus,
    spawned_agents: dict[str, AutonomousAgent],
    agent_name: str,
) -> dict[str, Callable[..., Any]]:
    provider_factory = _make_teammate_provider_factory(config)

    def _teammate_task_id(teammate_name: str) -> str:
        return f"team:{runtime_id}:{teammate_name}"

    def _team_list() -> list[dict[str, object]]:
        return [
            {
                **teammate.to_dict(),
                # Source of truth is the live background thread, not the
                # spawned_agents dict (which never prunes crashed/finished
                # teammates).
                "running": bg_manager.is_running(_teammate_task_id(teammate.name)),
            }
            for teammate in teammate_manager.list()
        ]

    def _team_send(to_agent: str, content: str) -> str:
        normalized_target = to_agent.strip()
        if normalized_target != MAIN_AGENT_NAME:
            teammate_manager.get(normalized_target)
        message_id = message_bus.send(
            Message(
                id="",
                from_agent=agent_name,
                to_agent=normalized_target,
                content=content,
                msg_type="request",
                timestamp="",
            )
        )
        # The main agent has no autonomous responder, so never block on it.
        if normalized_target == MAIN_AGENT_NAME:
            return f"Sent message {message_id} to {normalized_target}."
        # A teammate that is not running will never reply; return now instead of
        # waiting out the full timeout. (If it is spawned later, a reply would
        # surface via the mailbox drain on a subsequent turn.)
        if not bg_manager.is_running(_teammate_task_id(normalized_target)):
            return (
                f"Sent message {message_id} to {normalized_target}, but it is not "
                "running. Spawn it first (team_spawn / /team spawn) to get a reply."
            )
        # Block for the reply and hand it back to the caller. Mark the response
        # delivered so the polling drain does not surface it to the LLM twice.
        timeout = config.team.response_timeout
        response = ProtocolFSM(message_bus, agent_name).wait_response(
            message_id, timeout=timeout
        )
        if response is None:
            return (
                f"Sent message {message_id} to {normalized_target}; no reply within "
                f"{timeout:.0f}s. It may still be working -- a late reply will "
                "surface on a later turn."
            )
        message_bus.mark_delivered(response.id)
        _, body = decode_protocol_content(response.content)
        return f"Reply from {normalized_target}: {body}"

    def _team_spawn(name: str) -> str:
        teammate_name = name.strip()
        agent_instance = teammate_manager.spawn(teammate_name, provider_factory)
        message_bus.ensure_mailbox(teammate_name)
        teammate_permission = permission.clone(fail_closed=True)
        agent_handlers = _build_handlers(
            workspace_path=workspace_path,
            todo_manager=todo_manager,
            task_manager=task_manager,
            skill_loader=skill_loader,
            provider=agent_instance.provider,
            tools=tools,
            permission=teammate_permission,
            bg_manager=bg_manager,
            messages=[],
            config=config,
            runtime_id=runtime_id,
            teammate_manager=teammate_manager,
            message_bus=message_bus,
            spawned_agents=spawned_agents,
            agent_name=teammate_name,
            system_prompt_override=agent_instance.system_prompt,
        )
        # Conversational memory across requests (task 06-08): inject a per-teammate
        # Compactor (its own provider, no transcript persistence) so the growing
        # history stays bounded. Disabled -> no-op compaction + stateless behavior.
        memory_enabled = config.team.memory_enabled
        teammate_compact_fn = None
        if memory_enabled:
            teammate_compact_fn = Compactor(
                provider=agent_instance.provider,
                transcript_mgr=None,
                session_id=f"team:{teammate_name}",
            )
        autonomous_agent = AutonomousAgent(
            name=agent_instance.name,
            provider=agent_instance.provider,
            tools=tools,
            handlers=agent_handlers,
            bus=message_bus,
            task_manager=task_manager,
            permission=teammate_permission,
            system_prompt=agent_instance.system_prompt,
            poll_interval=config.team.poll_interval,
            compact_fn=teammate_compact_fn,
            memory_enabled=memory_enabled,
        )
        try:
            bg_manager.submit(_teammate_task_id(teammate_name), autonomous_agent.run)
        except ValueError:
            return f"Teammate {teammate_name} is already running."
        spawned_agents[teammate_name] = autonomous_agent
        return f"Spawned teammate {teammate_name} ({agent_instance.role})"

    def _team_shutdown(name: str) -> str:
        teammate_name = name.strip()
        if not teammate_name:
            return "Error: teammate name must not be empty."
        if not bg_manager.is_running(_teammate_task_id(teammate_name)):
            spawned_agents.pop(teammate_name, None)
            return f"Teammate {teammate_name} is not running."
        # SHUTDOWN is honored regardless of msg_type (checked before the request
        # filter in AutonomousAgent._handle_messages); wait_for_message wakes the
        # daemon immediately so it stops promptly.
        ProtocolFSM(message_bus, agent_name).request(
            teammate_name, Protocol.SHUTDOWN, "Stop requested."
        )
        spawned_agents.pop(teammate_name, None)
        return f"Sent shutdown to teammate {teammate_name}."

    def _team_register(
        name: str = "",
        role: str = "",
        system_prompt: str = "",
        provider: str = "",
        model: str = "",
    ) -> str:
        # Build a sparse provider override; an empty config inherits the session
        # provider (the teammate provider factory fills the gaps).
        provider_config: dict[str, Any] = {}
        provider_name = (provider or "").strip()
        model_id = (model or "").strip()
        if provider_name:
            provider_config["name"] = provider_name
        if model_id:
            provider_config["model"] = model_id
        try:
            teammate = teammate_manager.register(
                name, role, system_prompt, provider_config=provider_config or None
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return (
            f"Registered teammate {teammate.name} ({teammate.role}). "
            "Spawn it with team_spawn to start it."
        )

    def _team_request_review(to_agent: str, plan: str) -> str:
        normalized_target = to_agent.strip()
        if not normalized_target:
            return "Error: to_agent must not be empty."
        if not isinstance(plan, str) or not plan.strip():
            return "Error: plan must not be empty."
        # The main agent has no autonomous responder, so it cannot review.
        if normalized_target == MAIN_AGENT_NAME:
            return "Cannot request review from the main agent (no autonomous responder)."
        try:
            teammate_manager.get(normalized_target)
        except ValueError:
            return (
                f"Error: unknown teammate {normalized_target}. "
                "Register it first with team_register."
            )
        # A teammate that is not running will never reply; return now instead of
        # waiting out the full timeout (mirrors team_send).
        if not bg_manager.is_running(_teammate_task_id(normalized_target)):
            return (
                f"Teammate {normalized_target} is not running. Spawn it first "
                "(team_spawn / /team spawn) to request a review."
            )
        # Send a PLAN_APPROVAL protocol request (the receiving teammate wraps it as
        # a plan-review prompt) and block for the verdict, deduping the reply so the
        # mailbox drain does not surface it to the LLM twice.
        fsm = ProtocolFSM(message_bus, agent_name)
        message_id = fsm.request(normalized_target, Protocol.PLAN_APPROVAL, plan)
        timeout = config.team.response_timeout
        response = fsm.wait_response(message_id, timeout=timeout)
        if response is None:
            return (
                f"Sent review request {message_id} to {normalized_target}; no verdict "
                f"within {timeout:.0f}s. It may still be reviewing -- a late reply will "
                "surface on a later turn."
            )
        message_bus.mark_delivered(response.id)
        _, verdict = decode_protocol_content(response.content)
        return f"Review verdict from {normalized_target}: {verdict}"

    return {
        "team_list": _team_list,
        "team_send": _team_send,
        "team_spawn": _team_spawn,
        "team_shutdown": _team_shutdown,
        "team_register": _team_register,
        "team_request_review": _team_request_review,
    }


def _make_teammate_provider_factory(config: Config):
    def _factory(provider_overrides: dict[str, object]) -> BaseLLMProvider:
        provider_name = str(provider_overrides.get("name", config.provider.name)).strip()
        resolved_provider_name = provider_name or config.provider.name
        inherited_api_key_env = (
            config.provider.api_key_env
            if resolved_provider_name == config.provider.name
            else _default_api_key_env(resolved_provider_name)
        )
        api_key_env = (
            str(
                provider_overrides.get(
                    "api_key_env",
                    inherited_api_key_env,
                )
            ).strip()
            or inherited_api_key_env
        )
        provider_config = ProviderConfig(
            name=resolved_provider_name,
            model=str(provider_overrides.get("model", config.provider.model)).strip()
            or config.provider.model,
            api_key_env=api_key_env,
            base_url=_coerce_optional_string(
                provider_overrides.get("base_url", config.provider.base_url)
            ),
            wire_api=_coerce_optional_string(
                provider_overrides.get("wire_api", config.provider.wire_api)
            ),
        )
        return create_provider(
            SimpleNamespace(
                provider=provider_config,
                thinking=ThinkingConfig(
                    mode=config.thinking.mode,
                    budget_tokens=config.thinking.budget_tokens,
                ),
                cache=CacheConfig(
                    enabled=config.cache.enabled,
                    ttl=config.cache.ttl,
                ),
            )
        )

    return _factory


def _handle_team_command(
    text: str,
    ui_console: AgentConsole,
    *,
    teammate_manager: TeammateManager,
    team_handlers: dict[str, Callable[..., Any]],
) -> None:
    _, _, raw_args = text.partition(" ")
    parts = raw_args.split(" ", 2) if raw_args else []
    subcommand = parts[0] if parts else "list"

    try:
        if subcommand == "list":
            teammates = team_handlers["team_list"]()  # type: ignore[index, operator]
            if not teammates:
                ui_console.print_status("No teammates registered.")
                return
            for teammate in teammates:
                name = str(teammate.get("name", "unknown"))
                role = str(teammate.get("role", ""))
                running = "running" if teammate.get("running") else "idle"
                ui_console.console.print(f"{name} [{running}] - {role}")
            return

        if subcommand == "spawn" and len(parts) >= 2:
            result = team_handlers["team_spawn"](parts[1])  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return

        if subcommand == "send" and len(parts) >= 3:
            target = parts[1].strip()
            content = parts[2].strip()
            if not target or not content:
                raise ValueError("Usage: /team send <name> <message>")
            result = team_handlers["team_send"](target, content)  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return

        if subcommand == "shutdown" and len(parts) >= 2:
            result = team_handlers["team_shutdown"](parts[1])  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return

        if subcommand == "register":
            # name + role + system_prompt (the prompt is free text with spaces).
            reg_parts = raw_args.split(" ", 3)
            if len(reg_parts) < 4 or not reg_parts[1].strip() or not reg_parts[3].strip():
                raise ValueError("Usage: /team register <name> <role> <system_prompt>")
            result = team_handlers["team_register"](  # type: ignore[index, operator]
                reg_parts[1], reg_parts[2], reg_parts[3]
            )
            ui_console.print_status(str(result))
            return

        if subcommand == "review" and len(parts) >= 3:
            target = parts[1].strip()
            plan = parts[2].strip()
            if not target or not plan:
                raise ValueError("Usage: /team review <name> <plan>")
            result = team_handlers["team_request_review"](target, plan)  # type: ignore[index, operator]
            ui_console.print_status(str(result))
            return
    except Exception as exc:
        ui_console.print_error(str(exc))
        return

    ui_console.print_status(
        "Usage: /team list | /team spawn <name> | /team send <name> <message> "
        "| /team shutdown <name> | /team register <name> <role> <system_prompt> "
        "| /team review <name> <plan>"
    )


_MCP_PROMPT_USAGE = "Usage: /mcp:<server>:<prompt> [key=value ...]"
_MCP_COMMAND_USAGE = "Usage: /mcp <status|list|reload <name>>"


def _dispatch_mcp_command(
    text: str,
    *,
    mcp_manager: MCPManager,
    ui_console: AgentConsole,
) -> None:
    """Handle the space-prefixed ``/mcp <subcommand>`` REPL command.

    Returns nothing — feedback goes through ``ui_console``. Unknown
    subcommands or missing args become ``print_error`` lines and never raise.
    """
    tokens = text.split()
    if len(tokens) <= 1:
        ui_console.print_status(_MCP_COMMAND_USAGE)
        return
    sub = tokens[1]
    if sub == "status":
        rows = mcp_manager.summarize()
        if not rows:
            ui_console.print_status("(no MCP servers configured)")
            return
        for row in rows:
            resources_label = "resources" if row["has_resources"] else "no-resources"
            ui_console.print_status(
                f"{row['name']}: {row['status']} "
                f"[{row['tool_count']} tools, "
                f"{resources_label}, "
                f"{row['prompt_count']} prompts]"
            )
        return
    if sub == "list":
        any_server = False
        for name, client in mcp_manager.iter_running_clients():
            any_server = True
            ui_console.print_status(f"[{name}]")
            cached_tools = getattr(client, "_tools_cache", None) or []
            for tool in cached_tools:
                tool_name = tool.get("name") if isinstance(tool, dict) else None
                if not tool_name:
                    continue
                ui_console.print_status(f"  mcp__{name}__{tool_name}")
            if client.has_capability("resources"):
                ui_console.print_status(f"  mcp__{name}__resource_list")
                ui_console.print_status(f"  mcp__{name}__resource_read")
            cached_prompts = getattr(client, "_prompts", None) or []
            for prompt in cached_prompts:
                prompt_name = prompt.get("name") if isinstance(prompt, dict) else None
                if not prompt_name:
                    continue
                ui_console.print_status(f"  /mcp:{name}:{prompt_name}")
        if not any_server:
            ui_console.print_status("(no MCP servers running)")
        return
    if sub == "reload":
        if len(tokens) < 3:
            ui_console.print_error("Usage: /mcp reload <name>")
            return
        target = tokens[2]
        try:
            mcp_manager.reload(target)
        except MCPError as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"Server {target!r} is now UNHEALTHY.")
            return
        except Exception as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"Server {target!r} is now UNHEALTHY.")
            return
        ui_console.print_status(f"Server {target!r} reloaded.")
        return
    ui_console.print_error(f"Unknown /mcp subcommand: {sub}. Use status, list, or reload.")


def _parse_mcp_prompt_command(text: str) -> tuple[str, str, dict[str, str]] | None:
    """Parse ``/mcp:<server>:<prompt> [k=v ...]`` into (server, prompt, args).

    Returns ``None`` if the command is malformed; logging is the caller's job
    so callers can surface UI feedback consistently.
    """
    if not text.startswith("/mcp:"):
        return None
    rest = text[len("/mcp:") :]
    head, _, tail = rest.partition(" ")
    if ":" not in head:
        return None
    server_name, prompt_name = head.split(":", 1)
    server_name = server_name.strip()
    prompt_name = prompt_name.strip()
    if not server_name or not prompt_name:
        return None
    arguments: dict[str, str] = {}
    for tok in tail.split():
        if "=" not in tok:
            _log.warning("Ignoring malformed /mcp: argument %r (expected key=value)", tok)
            continue
        k, _sep, v = tok.partition("=")
        if not k:
            _log.warning("Ignoring malformed /mcp: argument %r (empty key)", tok)
            continue
        arguments[k] = v
    return server_name, prompt_name, arguments


def _dispatch_mcp_prompt(
    text: str,
    *,
    mcp_manager: MCPManager,
    messages: list[dict[str, Any]],
    ui_console: AgentConsole,
) -> bool:
    """Handle a ``/mcp:`` slash command. Returns True if messages were appended.

    On success the parsed ``prompts/get`` result is converted into transcript
    messages and appended; the caller is then expected to trigger the next
    ``agent_loop()`` iteration just like a normal user input.
    """
    parsed = _parse_mcp_prompt_command(text)
    if parsed is None:
        ui_console.print_error(_MCP_PROMPT_USAGE)
        return False
    server_name, prompt_name, arguments = parsed

    client = mcp_manager.get_client(server_name)
    if client is None:
        ui_console.print_error(f"Error: MCP server {server_name!r} is not running")
        return False
    if not client.has_capability("prompts"):
        ui_console.print_error(f"Error: server {server_name!r} does not support prompts")
        return False

    try:
        result = client.get_prompt(prompt_name, arguments)
    except MCPCallError as exc:
        ui_console.print_error(str(exc))
        return False
    except Exception as exc:  # pragma: no cover — defensive
        ui_console.print_error(f"Error: {type(exc).__name__}: {exc}")
        return False

    raw_messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(raw_messages, list) or not raw_messages:
        ui_console.print_error(
            f"Error: prompt {prompt_name!r} from {server_name!r} returned no messages"
        )
        return False

    appended = False
    for entry in raw_messages:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if isinstance(content, list):
            blocks = content
        elif isinstance(content, dict):
            blocks = [content]
        elif isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        else:
            blocks = []
        text_body = _mcp_flatten_content(blocks)
        messages.append({"role": role, "content": text_body})
        appended = True

    return appended


def _drain_team_mailbox(
    ui_console: AgentConsole,
    *,
    message_bus: MessageBus,
    since: str | None,
    sink: list[str] | None = None,
) -> str | None:
    """Surface new main-mailbox messages to the console and (optionally) the LLM.

    Messages already delivered out of band (a blocking ``team_send`` that
    returned the reply to the caller) are skipped so the LLM never sees them
    twice. When ``sink`` is provided, each surfaced message's text is appended to
    it; the REPL prepends those onto the next user turn so late / unsolicited
    teammate replies enter the conversation without breaking role alternation.
    """
    messages = message_bus.receive(MAIN_AGENT_NAME, since_id=since)
    if not messages:
        return since

    for message in messages:
        if message_bus.was_delivered(message.id):
            continue
        protocol, content = decode_protocol_content(message.content)
        label = f"Team {message.msg_type} from {message.from_agent}"
        if protocol is not None:
            label = f"{label} [{protocol.value}]"
        ui_console.print_status(f"{label}: {content}")
        if sink is not None:
            sink.append(f"[{label}] {content}")

    return messages[-1].id


def _broadcast_team_shutdown(message_bus: MessageBus) -> None:
    ProtocolFSM(message_bus, MAIN_AGENT_NAME).broadcast(
        Protocol.SHUTDOWN,
        "BareAgent main session is shutting down.",
    )


def _read_stdio_input() -> str:
    prompt = "bareagent> " if sys.stdin.isatty() and sys.stdout.isatty() else ""
    return input(prompt)


def _cycle_permission_mode(permission: PermissionGuard) -> PermissionMode:
    current_index = _MODE_CYCLE.index(permission.mode)
    next_mode = _MODE_CYCLE[(current_index + 1) % len(_MODE_CYCLE)]
    permission.mode = next_mode
    return next_mode


def _build_stdio_read_fn(
    workspace_path: Path,
    permission: PermissionGuard,
) -> Callable[[], str]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _read_stdio_input

    try:
        from src.ui.prompt import AgentPrompt

        agent_prompt = AgentPrompt(
            commands=list(_SLASH_COMMANDS),
            history_file=workspace_path / ".bareagent_history",
            get_mode_label=lambda: permission.mode.value.upper(),
            cycle_mode=lambda: _cycle_permission_mode(permission).value.upper(),
        )
        return agent_prompt.read_input
    except Exception:
        return lambda: input(f"[{permission.mode.value.upper()}] bareagent> ")


def _clear_stdio_screen(ui_console: AgentConsole) -> None:
    if getattr(ui_console.console, "is_terminal", False):
        ui_console.console.clear(home=True)


def _print_stdio_user_message(ui_console: AgentConsole, text: str) -> None:
    if not text.strip():
        return

    from src.ui.theme import get_theme

    ui_console.console.print(
        f"> {text}",
        style=f"bold {get_theme().palette.accent}",
        markup=False,
    )


def _replay_stdio_transcript(
    messages: list[dict[str, Any]],
    ui_console: AgentConsole,
) -> None:
    tool_name_by_id: dict[str, str] = {}

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            continue

        if role == "user":
            if isinstance(content, str):
                _print_stdio_user_message(ui_console, content)
                continue
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_name = tool_name_by_id.get(
                        str(block.get("tool_use_id", "")),
                        "unknown",
                    )
                    ui_console.print_tool_result(tool_name, block.get("content", ""))
                continue

        if role != "assistant":
            continue

        if isinstance(content, str):
            ui_console.print_assistant(content)
            continue
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_value = str(block.get("text", ""))
                if text_value:
                    text_parts.append(text_value)
                continue
            if block.get("type") != "tool_use":
                continue

            tool_name = str(block.get("name", "unknown"))
            tool_id = str(block.get("id", ""))
            if tool_id:
                tool_name_by_id[tool_id] = tool_name

            if text_parts:
                ui_console.print_assistant("\n".join(text_parts))
                text_parts = []
            ui_console.print_tool_call(tool_name, block.get("input", {}))

        if text_parts:
            ui_console.print_assistant("\n".join(text_parts))


def _handle_mode_selection_stdio(
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> None:
    lines = ["Permission modes:"]
    for idx, mode in enumerate(_MODE_CYCLE, 1):
        marker = "*" if mode == permission.mode else " "
        lines.append(f"  {marker} {idx}) {mode.value:<10} {_MODE_DESCRIPTIONS[mode]}")
    ui_console.print_status("\n".join(lines))
    ui_console.print_status(f"Select [1-{len(_MODE_CYCLE)}] on the next prompt.")
    valid_choices = {str(i) for i in range(1, len(_MODE_CYCLE) + 1)}
    try:
        choice = _read_stdio_input().strip()
    except (EOFError, KeyboardInterrupt):
        ui_console.print_status("Mode selection cancelled.")
        return

    if choice in valid_choices:
        old = permission.mode
        permission.mode = _MODE_CYCLE[int(choice) - 1]
        ui_console.print_status(f"Permission mode: {old.value} → {permission.mode.value}")
        return

    ui_console.print_status("Invalid choice, mode unchanged.")


def _make_plan_approval(
    permission: PermissionGuard,
    ui_console: AgentConsole,
) -> Callable[[str], PlanDecision]:
    """Build the ``exit_plan_mode`` approval callback.

    Renders the proposed plan, then prompts the user for a three-way decision
    (approve -> DEFAULT, approve+auto -> AUTO, reject -> stay in PLAN). On reject
    it collects an optional free-text reason fed back to the model. This wiring
    layer owns the permission-mode flip because it holds both the
    ``PermissionGuard`` and the UI console; the handler stays pure.
    """

    def _approve(plan: str) -> PlanDecision:
        # Defensive: the directive is injected only in PLAN mode, so a
        # well-behaved model won't call exit_plan_mode otherwise. If it does,
        # report a no-op rather than silently flipping an unrelated mode.
        if permission.mode != PermissionMode.PLAN:
            return PlanDecision("noop")
        # Approving a plan elevates the permission mode, which is a form of
        # approval -- a fail-closed guard must never grant it (error-handling.md).
        # This path only ever holds the main guard (fail_closed=False), but the
        # check keeps the invariant if the wiring ever changes.
        if getattr(permission, "fail_closed", False) or not sys.stdin.isatty():
            return PlanDecision("unavailable")

        ui_console.print_status("Proposed plan:")
        ui_console.print_assistant(plan)
        ui_console.print_status(
            "Approve this plan?\n"
            "  1) Approve -- switch to DEFAULT (writes still confirmed)\n"
            "  2) Approve & auto-accept -- switch to AUTO\n"
            "  3) Reject -- stay in plan mode and revise"
        )
        try:
            choice = _read_stdio_input().strip()
        except (EOFError, KeyboardInterrupt):
            ui_console.print_status("Plan approval cancelled; staying in plan mode.")
            return PlanDecision("reject")

        if choice == "1":
            _switch_mode_after_approval(permission, ui_console, PermissionMode.DEFAULT)
            return PlanDecision("approve-default")
        if choice == "2":
            _switch_mode_after_approval(permission, ui_console, PermissionMode.AUTO)
            return PlanDecision("approve-auto")

        # Anything else counts as reject. Collect an optional reason so the model
        # can revise with guidance instead of guessing.
        ui_console.print_status("Plan rejected. Optionally enter a reason (blank to skip):")
        try:
            reason = _read_stdio_input().strip()
        except (EOFError, KeyboardInterrupt):
            reason = ""
        return PlanDecision("reject", reason)

    return _approve


def _switch_mode_after_approval(
    permission: PermissionGuard,
    ui_console: AgentConsole,
    new_mode: PermissionMode,
) -> None:
    old = permission.mode
    permission.mode = new_mode
    ui_console.print_status(f"Permission mode: {old.value} -> {new_mode.value}")


def _install_plan_handler(
    handlers: dict[str, Any],
    plan_approval: Callable[[str], PlanDecision],
) -> None:
    """Register the exit_plan_mode handler on a (re)built handler dict.

    Called after every ``_build_handlers`` in the main loop -- session switches
    (/new, /compact, /resume, /import) rebuild ``handlers`` from scratch and
    would otherwise drop the main-loop-only exit_plan_mode handler, leaving its
    schema in ``tools`` with no handler behind it.
    """
    handlers["exit_plan_mode"] = partial(run_exit_plan_mode, approve_fn=plan_approval)


def _run_node_batch(
    thunks: list[Callable[[], NodeResult]],
    max_concurrency: int,
) -> list[NodeResult]:
    """Run a batch of (never-raising) node thunks concurrently, preserving order.

    A single node skips the thread-pool overhead. ``KeyboardInterrupt`` (delivered
    to this main thread while blocked on results) cancels pending nodes and tears
    the executor down without waiting, then propagates so the workflow tool call
    aborts cleanly (in-flight nodes finish in the background). Worker threads only
    run ``run_subagent`` (which is silent -- no console / messages), mirroring how
    the ``/loop`` scheduler keeps execution off the REPL-owned UI thread.
    """
    if not thunks:
        return []
    if len(thunks) == 1:
        return [thunks[0]()]

    workers = max(1, min(max_concurrency, len(thunks)))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-node")
    futures = [executor.submit(thunk) for thunk in thunks]
    try:
        return [future.result() for future in futures]
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=False)


def _install_workflow_handler(
    handlers: dict[str, Any],
    *,
    enabled: bool,
    provider: BaseLLMProvider,
    base_tools: list[dict[str, Any]],
    permission: PermissionGuard,
    bg_manager: BackgroundManager,
    console: AgentConsole,
    retry_policy: RetryPolicy,
    max_depth: int,
    default_agent_type: str,
    max_concurrency: int,
    max_nodes: int,
) -> None:
    """Install the main-loop-only ``workflow`` handler on a (re)built handler dict.

    Like ``_install_plan_handler``, this is re-run after every ``_build_handlers``
    (session switches rebuild ``handlers`` from scratch). It binds the node
    executor to *this* handler dict so nodes inherit the current tools/handlers;
    ``run_subagent`` filters the main-loop-only tools (``workflow`` itself,
    ``exit_plan_mode``) back out per node. Disabled config -> no install (the
    schema is also withheld from ``loop_tools``), so the feature fully short
    -circuits. Node subagents run fail-closed (no prompts from worker threads),
    the same unattended stance as background subagents and ``/loop``.
    """
    if not enabled:
        return
    node_handlers = handlers

    def execute_node(node: WorkflowNode, upstream: dict[str, NodeResult]) -> str:
        node_permission: Any = permission
        if isinstance(permission, PermissionGuard):
            node_permission = permission.clone(fail_closed=True)
        return run_subagent(
            provider=provider,
            task=build_node_prompt(node, upstream),
            tools=base_tools,
            handlers=node_handlers,
            permission=node_permission,
            max_depth=max_depth,
            agent_type=node.agent_type,
            bg_manager=bg_manager,
            default_agent_type=default_agent_type,
            retry_policy=retry_policy,
        )

    def handler(**kwargs: Any) -> str:
        return run_workflow_tool(
            nodes=kwargs.get("nodes"),
            execute_node=execute_node,
            map_concurrent=lambda thunks: _run_node_batch(thunks, max_concurrency),
            on_progress=console.print_status,
            max_nodes=max_nodes,
        )

    handlers["workflow"] = handler


def _install_subagent_send_handler(
    handlers: dict[str, Any],
    *,
    registry: SubagentRegistry,
) -> None:
    """Install the main-loop-only ``subagent_send`` handler on a (re)built dict.

    Like ``_install_workflow_handler`` / ``_install_plan_handler``, this re-runs
    after every ``_build_handlers`` (session switches rebuild ``handlers`` from
    scratch). Everything needed to re-enter ``agent_loop`` lives in the stored
    ``ResumableContext`` (provider / tools / handlers / permission / compactor /
    turn budget / retry policy), so the closure only needs the registry. The
    schema is also kept out of the base ``tools`` and listed in
    ``MAIN_LOOP_ONLY_TOOLS``, so no sub-agent ever sees this tool.
    """

    def run_loop(context: ResumableContext) -> str:
        return agent_loop(
            provider=context.provider,
            messages=context.messages,
            tools=context.tools,
            handlers=context.handlers,
            permission=context.permission,
            compact_fn=context.compact_fn,
            bg_manager=None,
            max_iterations=context.max_turns,
            retry_policy=context.retry_policy,
        )

    def handler(agent_id: str = "", message: str = "", **_: Any) -> str:
        return run_subagent_send(
            agent_id,
            message,
            registry=registry,
            run_loop=run_loop,
        )

    handlers["subagent_send"] = handler


def _install_mcp_cleanup(mcp_manager: MCPManager) -> None:
    """Register exit-time + SIGTERM hooks so MCP subprocesses are reaped.

    ``atexit`` catches ``return`` from ``main()`` and any ``sys.exit()``;
    the SIGTERM handler converts a polite termination into ``sys.exit(130)``
    so ``atexit`` actually fires (raw SIGTERM bypasses it). SIGINT is
    intentionally left alone — prompt-toolkit + the existing
    ``KeyboardInterrupt`` handling in the REPL loop already cover Ctrl+C.
    """
    atexit.register(mcp_manager.close_all)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    except (ValueError, OSError):  # pragma: no cover — non-main thread / unsupported OS
        pass


def _install_lsp_cleanup(lsp_manager: LanguageServerManager) -> None:
    """Register an idempotent atexit hook for the LSP manager.

    Deliberately decoupled from :func:`_install_mcp_cleanup`: ``atexit`` fires
    callbacks LIFO regardless of registration order, and
    ``lsp_manager.close_all`` is guaranteed idempotent (see manager docstring)
    so duplicate registration from a future caller is safe. The SIGTERM
    handler is already installed by the MCP path; we rely on that to convert
    the signal into ``sys.exit(130)`` so this atexit hook actually fires.
    """
    atexit.register(lsp_manager.close_all)


_LSP_COMMAND_USAGE = "Usage: /lsp <status|list|reload <language>>"


def _dispatch_lsp_command(
    text: str,
    *,
    lsp_manager: LanguageServerManager,
    ui_console: AgentConsole,
) -> None:
    """Handle the space-prefixed ``/lsp <subcommand>`` REPL command.

    Mirrors the ``/mcp`` command shape: ``status`` shows per-server health,
    ``list`` enumerates the ``lsp_*`` tools available right now (only for
    RUNNING servers), and ``reload <language>`` rebuilds one server.
    Feedback flows through ``ui_console``; the routine never raises.
    """
    tokens = text.split()
    if len(tokens) <= 1:
        ui_console.print_status(_LSP_COMMAND_USAGE)
        return
    sub = tokens[1]
    if sub == "status":
        rows = lsp_manager.summarize()
        if not rows:
            ui_console.print_status("(no LSP servers configured)")
            return
        for row in rows:
            extensions = ", ".join(row["extensions"]) or "-"
            line = (
                f"{row['language']}: {row['status']} [{row['tool_count']} tools, ext={extensions}]"
            )
            reason = row.get("reason") or ""
            if reason:
                line = f"{line} — {reason}"
            ui_console.print_status(line)
        return
    if sub == "list":
        any_server = False
        for language, _server in lsp_manager.iter_running():
            any_server = True
            ui_console.print_status(f"[{language}]")
            # The four Tier-1 tools are uniform across servers; list them so
            # users see exactly what the LLM has access to right now.
            for tool in (
                "lsp_outline",
                "lsp_definition",
                "lsp_references",
                "lsp_diagnostics",
            ):
                ui_console.print_status(f"  {tool}")
        if not any_server:
            ui_console.print_status("(no LSP servers running)")
        return
    if sub == "reload":
        if len(tokens) < 3:
            ui_console.print_error("Usage: /lsp reload <language>")
            return
        target = tokens[2]
        try:
            lsp_manager.reload(target)
        except LSPError as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"LSP server {target!r} is now UNHEALTHY.")
            return
        except Exception as exc:
            ui_console.print_error(f"reload {target!r} failed: {exc}")
            ui_console.print_error(f"LSP server {target!r} is now UNHEALTHY.")
            return
        ui_console.print_status(f"LSP server {target!r} reloaded.")
        return
    ui_console.print_error(f"Unknown /lsp subcommand: {sub}. Use status, list, or reload.")


_LOOP_COMMAND_USAGE = (
    "Usage:\n"
    "  /loop <seconds> <command...>   Schedule a command to repeat every N seconds\n"
    "  /loop list                     List scheduled commands\n"
    "  /loop cancel <job_id>          Cancel one scheduled command\n"
    "  /loop clear                    Cancel all scheduled commands\n"
    "Note: scheduled commands run WITHOUT permission prompts (no human to confirm)."
)


def _dispatch_loop_command(
    text: str,
    *,
    scheduler: Scheduler,
    ui_console: AgentConsole,
) -> None:
    """Handle the ``/loop`` REPL command for the cron-style scheduler.

    Forms: ``/loop`` / ``/loop list`` (list), ``/loop <seconds> <command...>``
    (create), ``/loop cancel <job_id>`` (cancel one), ``/loop clear`` (cancel
    all). Scheduled commands run via the background pool WITHOUT permission
    confirmation, so the create path echoes that warning. Never raises.
    """
    rest = text[len("/loop") :].strip()
    if not rest or rest == "list":
        jobs = scheduler.list()
        if not jobs:
            ui_console.print_status(f"(no scheduled commands)\n{_LOOP_COMMAND_USAGE}")
            return
        for job in jobs:
            ui_console.print_status(
                f"{job.job_id}: every {job.interval_sec:g}s, runs={job.run_count} — {job.command}"
            )
        return

    first, _, remainder = rest.partition(" ")
    if first == "cancel":
        job_id = remainder.strip()
        if not job_id:
            ui_console.print_error("Usage: /loop cancel <job_id>")
            return
        if scheduler.cancel(job_id):
            ui_console.print_status(f"Cancelled scheduled command: {job_id}")
        else:
            ui_console.print_error(f"No scheduled command found: {job_id}")
        return
    if first == "clear":
        scheduler.cancel_all()
        ui_console.print_status("Cancelled all scheduled commands.")
        return

    # Create form: first token is the interval in seconds, the rest is the
    # command verbatim (keep internal spaces).
    try:
        interval_sec = float(first)
    except ValueError:
        ui_console.print_error(
            f"Invalid interval {first!r}: expected a number of seconds.\n{_LOOP_COMMAND_USAGE}"
        )
        return
    command = remainder.strip()
    if not command:
        ui_console.print_error(f"Missing command to schedule.\n{_LOOP_COMMAND_USAGE}")
        return
    try:
        job = scheduler.add(interval_sec, command)
    except SchedulerError as exc:
        ui_console.print_error(str(exc))
        return
    ui_console.print_status(
        f"Scheduled {job.job_id}: every {job.interval_sec:g}s — {job.command}\n"
        "Warning: this command runs WITHOUT permission confirmation in the background."
    )


def _run_skill_reflection(
    *,
    provider: Any,
    messages: list[dict[str, Any]],
    store: SkillStore,
    skill_loader: SkillLoader,
    console: AgentConsole,
    token_tracker: Any,
    permission: Any,
    max_pending: int,
) -> None:
    """Reflect on the just-finished session and draft (or evolve) a skill.

    Runs an isolated ``agent_loop`` on a COPY of the conversation. ``skill_create``
    is the only write tool, so the real history / turn result stay clean and
    normal turns / sub-agents never see it. When generated skills already exist,
    the reflection additionally lists them as refinement targets and exposes
    read-only ``load_skill`` so the model can read one and supersede it with an
    improved same-name draft (self-evolution). Canon (repo) skill names are
    reserved so a generated skill never shadows them. The model may decline
    (reply "no skill"). Never raises — a reflection failure must not break the
    REPL.
    """
    console.print_status("Reflecting on this session to draft a reusable skill...")
    # Evolution candidates = generated *live* skills only (pending excluded by
    # the one-level glob; canon excluded by scanning the generated root alone).
    candidates = [(meta.skill_name, meta.description) for meta in SkillLoader(store.root).scan()]
    reserved_names = skill_loader.canon_skill_names()
    reflection_messages = list(messages)
    reflection_messages.append({"role": "user", "content": render_reflection_prompt(candidates)})
    draft_handlers: dict[str, Any] = {
        "skill_create": partial(run_skill_create, store=store, reserved_names=reserved_names)
    }
    draft_tools = [SKILL_CREATE_TOOL_SCHEMA]
    # Only offer the read tool when there is something to refine, so the no-skill
    # case stays byte-identical to the create-only behavior.
    if candidates:
        draft_tools = [SKILL_CREATE_TOOL_SCHEMA, *LOAD_SKILL_TOOL_SCHEMAS]
        draft_handlers["load_skill"] = skill_loader.load
    try:
        agent_loop(
            provider=provider,
            messages=reflection_messages,
            tools=draft_tools,
            handlers=draft_handlers,
            permission=permission,
            stream=False,
            console=console,
            max_iterations=6,
            token_tracker=token_tracker,
            skill_gen=None,
        )
    except (LLMCallError, KeyboardInterrupt):
        console.print_status("Skill reflection skipped (interrupted or LLM error).")
        return
    except Exception as exc:  # never let reflection break the REPL
        console.print_error(f"Skill reflection failed: {type(exc).__name__}: {exc}")
        return
    removed = store.prune_pending(max_pending)
    pending = store.list_pending()
    if pending:
        console.print_status(
            "Pending skills: "
            + ", ".join(pending)
            + " — /skill keep <name> to keep, /skill discard <name> to drop."
        )
    if removed:
        console.print_status(f"Pruned old pending drafts: {', '.join(removed)}")


def _run_goal_evaluator(
    *,
    provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    condition: str,
    console: AgentConsole,
    token_tracker: Any,
    permission: Any,
) -> Verdict:
    """Isolated evaluator: judge whether ``condition`` is met from the transcript.

    Mirrors :func:`_run_skill_reflection`: runs ``agent_loop`` on a COPY of the
    messages with ``goal_verdict`` as the only tool, so real history / turn
    results stay clean. Never raises for ordinary failures — an LLM error or a
    missing verdict yields a malformed (= not met) verdict so the loop falls
    through to its ``max_turns`` guard rather than crashing. ``KeyboardInterrupt``
    propagates so the user can abort the whole goal loop.
    """
    sink: list[Verdict] = []
    eval_messages = list(messages)
    eval_messages.append({"role": "user", "content": build_evaluator_prompt(condition)})
    try:
        agent_loop(
            provider=provider,
            messages=eval_messages,
            tools=[GOAL_VERDICT_TOOL_SCHEMA],
            handlers={"goal_verdict": partial(run_goal_verdict, sink=sink)},
            permission=permission,
            stream=False,
            console=console,
            max_iterations=3,
            token_tracker=token_tracker,
            skill_gen=None,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - evaluator failure must not break the loop
        console.print_error(f"Goal evaluator failed: {type(exc).__name__}: {exc}")
        return Verdict(met=False, reason="evaluator error", malformed=True)
    if not sink:
        return Verdict(met=False, reason="evaluator returned no verdict", malformed=True)
    return sink[-1]


def _drive_goal(
    command: Any,
    *,
    provider: BaseLLMProvider,
    evaluator_provider: BaseLLMProvider,
    messages: list[dict[str, Any]],
    loop_tools: list[dict[str, Any]],
    handlers: dict[str, Any],
    permission: PermissionGuard,
    compact_fn: Any,
    bg_manager: Any,
    config: Config,
    ui_console: AgentConsole,
    interaction_logger: Any,
    token_tracker: Any,
    hook_engine: Any,
    retry_policy: RetryPolicy,
    transcript_mgr: Any,
) -> None:
    """Run the synchronous self-driving goal loop for a parsed ``run`` command.

    Each turn runs the real ``agent_loop`` (sharing the main loop's tools /
    handlers / permission), then an isolated evaluator decides whether to stop.
    The loop respects the current permission mode (never auto-escalates); in
    DEFAULT it warns that writes still prompt each turn. ``skill_gen`` is omitted
    so the inner turns never trigger skill reflection. Interrupts / LLM errors
    roll back the in-flight turn and abort the loop, leaving completed turns.
    """
    state = GoalState(condition=command.condition, max_turns=command.max_turns)
    if permission.mode == PermissionMode.DEFAULT:
        ui_console.print_status(
            "Goal set in DEFAULT mode: write operations still prompt each turn. "
            "Use /auto first for an unattended run."
        )
    ui_console.print_status(f"Goal: {state.condition}  (max {state.max_turns} turns)")

    def run_turn(prompt: str) -> None:
        snapshot = len(messages)
        messages.append({"role": "user", "content": prompt})
        try:
            agent_loop(
                provider=provider,
                messages=messages,
                tools=loop_tools,
                handlers=handlers,
                permission=permission,
                compact_fn=compact_fn,
                bg_manager=bg_manager,
                stream=config.ui.stream,
                console=ui_console,
                interaction_logger=interaction_logger,
                token_tracker=token_tracker,
                hook_engine=hook_engine,
                retry_policy=retry_policy,
                skill_gen=None,
            )
            _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
        except (LLMCallError, KeyboardInterrupt):
            del messages[snapshot:]
            raise

    def evaluate() -> Verdict:
        return _run_goal_evaluator(
            provider=evaluator_provider,
            messages=messages,
            condition=state.condition,
            console=ui_console,
            token_tracker=token_tracker,
            permission=permission,
        )

    try:
        outcome, verdict = run_goal_loop(
            state,
            run_turn=run_turn,
            evaluate=evaluate,
            on_progress=ui_console.print_status,
        )
    except KeyboardInterrupt:
        ui_console.print_status(f"Goal aborted after {state.turns_used} turn(s).")
        return
    except LLMCallError:
        ui_console.print_error(
            f"Goal aborted: LLM call failed after {state.turns_used} turn(s)."
        )
        return

    if outcome is GoalOutcome.MET:
        ui_console.print_status(f"Goal met after {state.turns_used} turn(s).")
    else:
        last_reason = verdict.reason if verdict else ""
        msg = f"Goal not met after {state.turns_used} turn(s) (max turns reached)."
        if last_reason:
            msg += f" Last evaluator note: {last_reason}"
        ui_console.print_status(msg)


def _print_skill_list(store: SkillStore, loader: SkillLoader, console: AgentConsole) -> None:
    live = [meta.skill_name for meta in loader.scan()]
    live_set = set(live)
    pending = store.list_pending()
    lines = ["Loadable skills:"]
    lines += [f"  - {name}" for name in live] or ["  (none)"]
    lines.append("Pending drafts:")
    pending_lines = []
    for name in pending:
        # A pending draft whose name matches a loadable skill is a revision that
        # will REPLACE the live version on /skill keep (self-evolution).
        revision = f"  (revision of live '{name}')" if name in live_set else ""
        pending_lines.append(
            f"  - {name}{revision}  (/skill keep {name} | /skill discard {name})"
        )
    lines += pending_lines or ["  (none)"]
    console.print_status("\n".join(lines))


def _dispatch_skill_command(
    text: str,
    *,
    store: SkillStore,
    loader: SkillLoader,
    console: AgentConsole,
) -> None:
    """Handle ``/skill`` (``list`` | ``keep <name>`` | ``discard <name>``).

    Never raises — fails safe with an error line so a bad argument can't crash
    the REPL (same stance as the other ``_dispatch_*`` commands).
    """
    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"
    arg = parts[2] if len(parts) > 2 else ""
    try:
        if sub == "list":
            _print_skill_list(store, loader, console)
            return
        if sub == "keep":
            if not arg:
                console.print_error("Usage: /skill keep <name>")
                return
            console.print_status(store.promote(arg))
            # Refresh the cache so the promoted skill is loadable this session.
            loader.scan()
            return
        if sub == "discard":
            if not arg:
                console.print_error("Usage: /skill discard <name>")
                return
            console.print_status(store.discard(arg))
            return
        console.print_error(
            f"Unknown /skill subcommand {sub!r}. "
            "Use: /skill [list | keep <name> | discard <name>]."
        )
    except SkillStoreError as exc:
        console.print_error(f"Error: {exc}")
    except Exception as exc:  # never raise out of a slash command
        console.print_error(f"/skill failed: {type(exc).__name__}: {exc}")


def _dispatch_export_command(
    text: str,
    *,
    messages: list[dict[str, Any]],
    session_id: str,
    workspace_path: Path,
    ui_console: AgentConsole,
) -> None:
    """Handle the ``/export`` REPL command.

    Forms: ``/export`` (markdown default), ``/export <format>`` and
    ``/export <format> <path>`` where format ∈ {markdown, md, json}; the first
    token, when not a known format, is treated as an explicit path with the
    default markdown format. Markdown skips system messages and omits thinking;
    JSON is a faithful self-contained wrapper. Defaults to
    ``.transcripts/exports/<session>_<ts>.{md,json}``. Runs without permission
    confirmation (user-initiated, infrastructure tier). Never raises.
    """
    try:
        rest = text[len("/export") :].strip()
        parts = rest.split(maxsplit=1)
        if parts and parts[0] in ("markdown", "md", "json"):
            fmt = "markdown" if parts[0] in ("markdown", "md") else "json"
            user_path = parts[1].strip() if len(parts) > 1 else ""
        else:
            fmt = "markdown"
            user_path = rest

        ext = "json" if fmt == "json" else "md"
        if user_path:
            path = Path(user_path).expanduser()
            if not path.is_absolute():
                path = workspace_path / path
        else:
            timestamp = datetime.now().strftime(_SESSION_ID_TIMESTAMP_FORMAT)
            path = workspace_path / ".transcripts" / "exports" / f"{session_id}_{timestamp}.{ext}"

        if fmt == "json":
            content = to_export_json(
                messages,
                session_id=session_id,
                exported_at=datetime.now().isoformat(),
            )
        else:
            content = render_markdown(messages, title=f"Conversation {session_id}")

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
        ui_console.print_status(f"Exported to {path}")
    except Exception as exc:  # noqa: BLE001 - never crash the REPL on export
        ui_console.print_error(f"Export failed: {exc}")


def _run_stdio_session(
    config: Config,
    provider: BaseLLMProvider,
    *,
    workspace: Path | None = None,
    agent_console: AgentConsole | None = None,
) -> int:
    from src.ui.theme import init_theme

    init_theme(config.ui.theme)
    ui_console = agent_console or AgentConsole()
    ui_console.set_theme()
    workspace_path = (workspace or Path.cwd()).resolve()
    transcript_mgr = TranscriptManager(workspace_path / ".transcripts")
    session_id = _generate_session_id(transcript_mgr)
    interaction_logger = _build_interaction_logger(
        config,
        workspace_path,
        session_id,
    )
    _configure_tracing(
        config,
        session_id=session_id,
        interaction_logger=interaction_logger,
    )
    viewer_server = None
    token_tracker = TokenTracker()
    todo_manager = TodoManager()
    task_manager = _load_task_manager(workspace_path, ui_console)
    bg_manager = BackgroundManager()
    scheduler = Scheduler(
        runner=partial(run_bash, cwd=workspace_path, raise_on_error=True),
        notifier=bg_manager,
    )
    teammate_manager = _load_teammate_manager(workspace_path, ui_console)
    # Experiential skill generation: generated skills live under a project-
    # isolated user-global root (separate from the repo's checked-in canon).
    # SkillLoader scans both (canon wins on name conflicts); SkillStore owns the
    # pending drafts + promotion; SkillGenerator owns the trigger decision.
    generated_skills_root = resolve_generated_skills_root(workspace_path, config.skills.dir)
    skill_store = SkillStore(generated_skills_root)
    skill_loader = SkillLoader(resolve_skills_dir(), generated_root=generated_skills_root)
    skillgen_config = _build_skillgen_config(config.skills)
    skill_generator = SkillGenerator(skillgen_config) if skillgen_config.enabled else None
    memory_manager = _build_memory_manager(config, workspace_path, ui_console)
    message_bus, main_mailbox_cursor = _switch_session_mailbox(
        workspace_path,
        session_id,
    )
    spawned_agents: dict[str, AutonomousAgent] = {}
    # Resumable foreground subagents (task 06-06): session-scoped, in-memory,
    # one instance for the REPL lifetime. Cleared on /new / /resume / /import /
    # /clear (mirroring spawned_agents) and preserved across /compact.
    subagent_registry = SubagentRegistry(config.subagent.max_resumable)
    # Late / unsolicited teammate replies surfaced by the mailbox drain are
    # buffered here and prepended onto the next user turn (keeps role
    # alternation intact). Blocking team_send replies bypass this -- they return
    # straight to the LLM as the tool result.
    pending_team_messages: list[str] = []
    messages = _initial_messages(
        workspace_path,
        skill_summary=skill_loader.get_skill_list_prompt(),
        memory_context=_memory_context(memory_manager),
    )
    mcp_manager = MCPManager(config.mcp, console=ui_console, notifier=bg_manager)
    mcp_manager.start_all()
    _install_mcp_cleanup(mcp_manager)
    lsp_manager = LanguageServerManager(
        config.lsp,
        console=ui_console,
        repository_root=str(workspace_path),
        notifier=bg_manager,
    )
    lsp_manager.start_all()
    _install_lsp_cleanup(lsp_manager)
    tools = get_tools(mcp_manager, lsp_manager)
    permission = _build_permission_guard(config)
    _install_stdio_permission_prompt(permission, ui_console)
    read_fn = _build_stdio_read_fn(workspace_path, permission)
    base_compact_fn = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        session_id=session_id,
    )
    compact_fn = _build_loop_compact(
        base_compact_fn,
        todo_manager,
        memory_manager=memory_manager,
        recall_k=config.memory.recall_k,
        permission=permission,
    )
    # Hooks only fire in the main loop; sub-agents never receive the engine.
    hook_engine = HookEngine(config.hooks, console=ui_console)
    # Sub-agents *do* inherit the retry policy (D6) so background agents weather
    # transient failures too; threaded through _build_handlers -> get_handlers.
    retry_policy = _build_retry_policy(config.retry)
    # Provider for the /goal completion evaluator: a cheaper model if configured,
    # else the session provider (built once; reused across goal turns).
    goal_evaluator_provider = _build_goal_provider(config, provider)
    handlers = _build_handlers(
        workspace_path=workspace_path,
        todo_manager=todo_manager,
        task_manager=task_manager,
        skill_loader=skill_loader,
        provider=provider,
        tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        messages=messages,
        config=config,
        runtime_id=session_id,
        teammate_manager=teammate_manager,
        message_bus=message_bus,
        spawned_agents=spawned_agents,
        agent_name=MAIN_AGENT_NAME,
        mcp_manager=mcp_manager,
        lsp_manager=lsp_manager,
        memory_manager=memory_manager,
        subagent_registry=subagent_registry,
    )

    # Plan-mode workflow: exit_plan_mode is a main-loop-only tool. ``tools`` stays
    # the canonical base list fed to every _build_handlers call (sub-agent and
    # teammate closures inherit it, so they never see exit_plan_mode); the
    # augmented ``loop_tools`` is fed ONLY to the top-level agent_loop calls. Its
    # handler is installed on the live dict (and re-installed after every session
    # switch below, since those rebuild ``handlers`` from scratch). Defense in
    # depth: filter_tools also strips MAIN_LOOP_ONLY_TOOLS for every agent type
    # and filter_handlers drops the orphaned handler. ``plan_approval`` closes
    # over the permission guard + console, so the same callback is reused on every
    # rebuild.
    plan_approval = _make_plan_approval(permission, ui_console)
    loop_tools = [*tools, EXIT_PLAN_MODE_TOOL_SCHEMA]
    # Workflow orchestration (task 06-06): ``workflow`` is a main-loop-only tool
    # like ``exit_plan_mode`` -- its schema joins ``loop_tools`` (never the base
    # ``tools`` fed to sub-agent closures) and its handler is (re)installed after
    # every ``_build_handlers`` via ``install_workflow_handler``. When disabled the
    # schema is withheld and the install is a no-op, so the feature short-circuits.
    if config.workflow.enabled:
        loop_tools.append(WORKFLOW_TOOL_SCHEMA)
    # subagent_send (task 06-06): main-loop-only continuation tool. Always on
    # (no config gate); schema joins loop_tools only, handler re-installed after
    # every _build_handlers below. The registry instance is stable for the REPL
    # lifetime, so the bound install closure stays valid across rebuilds.
    loop_tools.append(SUBAGENT_SEND_TOOL_SCHEMA)
    install_subagent_send_handler = partial(
        _install_subagent_send_handler,
        registry=subagent_registry,
    )
    install_workflow_handler = partial(
        _install_workflow_handler,
        enabled=config.workflow.enabled,
        provider=provider,
        base_tools=tools,
        permission=permission,
        bg_manager=bg_manager,
        console=ui_console,
        retry_policy=retry_policy,
        max_depth=config.subagent.max_depth,
        default_agent_type=config.subagent.default_type,
        max_concurrency=config.workflow.max_concurrency,
        max_nodes=config.workflow.max_nodes,
    )
    _install_plan_handler(handlers, plan_approval)
    install_workflow_handler(handlers)
    install_subagent_send_handler(handlers)

    ui_console.console.print(
        f"BareAgent REPL ({config.provider.name}/{config.provider.model})",
        style="bold cyan",
    )
    ui_console.print_status(
        f"Permission mode: {permission.mode.value}. Type /help to see available commands."
    )

    # Passive config-change detection (ROADMAP 4.3): record the config files'
    # mtimes once at startup so we only nudge the user about *new* edits.
    last_config_mtimes = _config_mtimes(config)

    try:
        while True:
            main_mailbox_cursor = _drain_team_mailbox(
                ui_console,
                message_bus=message_bus,
                since=main_mailbox_cursor,
                sink=pending_team_messages,
            )
            current_config_mtimes = _config_mtimes(config)
            if current_config_mtimes != last_config_mtimes:
                last_config_mtimes = current_config_mtimes
                ui_console.print_status("config changed on disk — type /reload to apply")
            try:
                user_input = read_fn()
            except (KeyboardInterrupt, EOFError):
                _broadcast_team_shutdown(message_bus)
                ui_console.print_status("\nExiting BareAgent.")
                return 0

            text = user_input.strip()
            if not text:
                continue
            if text == "/exit":
                _broadcast_team_shutdown(message_bus)
                ui_console.print_status("Exiting BareAgent.")
                return 0
            if text == "/help":
                ui_console.print_status(_HELP_TEXT)
                continue
            if text in ("/clear", "/new"):
                if text == "/clear":
                    _clear_stdio_screen(ui_console)
                messages[:] = _initial_messages(
                    workspace_path,
                    skill_summary=skill_loader.get_skill_list_prompt(),
                    memory_context=_memory_context(memory_manager),
                )
                todo_manager.reset()
                token_tracker.reset()
                if skill_generator is not None:
                    skill_generator.reset()
                new_session_id = _generate_session_id(
                    transcript_mgr,
                    reserved_ids={_get_compact_session_id(compact_fn)},
                )
                _set_compact_session_id(compact_fn, new_session_id)
                _set_interaction_logger_session(interaction_logger, new_session_id)
                message_bus, main_mailbox_cursor = _switch_session_mailbox(
                    workspace_path,
                    new_session_id,
                    current_bus=message_bus,
                )
                spawned_agents = {}
                pending_team_messages.clear()
                subagent_registry.clear()
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=new_session_id,
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                    subagent_registry=subagent_registry,
                )
                _install_plan_handler(handlers, plan_approval)
                install_workflow_handler(handlers)
                install_subagent_send_handler(handlers)
                ui_console.print_status("New conversation started.")
                continue
            if text == "/compact":
                compact_fn(messages, force=True)
                _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                ui_console.print_status("Context compaction finished.")
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=_get_compact_session_id(compact_fn),
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                    subagent_registry=subagent_registry,
                )
                _install_plan_handler(handlers, plan_approval)
                install_workflow_handler(handlers)
                install_subagent_send_handler(handlers)
                continue
            if text == "/sessions":
                sessions = transcript_mgr.list_sessions()
                if not sessions:
                    ui_console.print_status("No saved sessions.")
                else:
                    for saved_session in sessions:
                        ui_console.console.print(saved_session)
                continue
            if text == "/resume" or text.startswith("/resume "):
                _, _, raw_session_id = text.partition(" ")
                requested_session = raw_session_id.strip() or None
                try:
                    restored_messages = transcript_mgr.resume(requested_session)
                except FileNotFoundError as exc:
                    ui_console.print_error(str(exc))
                    continue
                messages[:] = restored_messages
                token_tracker.reset()
                resumed_session = requested_session or transcript_mgr.get_latest_session()
                if resumed_session is not None:
                    _set_compact_session_id(compact_fn, resumed_session)
                    _set_interaction_logger_session(
                        interaction_logger,
                        resumed_session,
                    )
                    message_bus, main_mailbox_cursor = _switch_session_mailbox(
                        workspace_path,
                        resumed_session,
                        current_bus=message_bus,
                    )
                    spawned_agents = {}
                    pending_team_messages.clear()
                    subagent_registry.clear()
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=_get_compact_session_id(compact_fn),
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                    subagent_registry=subagent_registry,
                )
                _install_plan_handler(handlers, plan_approval)
                install_workflow_handler(handlers)
                install_subagent_send_handler(handlers)
                _replay_stdio_transcript(messages, ui_console)
                ui_console.print_status(f"Resumed session: {resumed_session}")
                continue
            if text == "/export" or text.startswith("/export "):
                _dispatch_export_command(
                    text,
                    messages=messages,
                    session_id=_get_compact_session_id(compact_fn),
                    workspace_path=workspace_path,
                    ui_console=ui_console,
                )
                continue
            if text == "/import" or text.startswith("/import "):
                _, _, raw_path = text.partition(" ")
                import_path = raw_path.strip()
                if not import_path:
                    ui_console.print_error("Usage: /import <path-to-.json-or-.jsonl>")
                    continue
                p = Path(import_path).expanduser()
                try:
                    raw_text = p.read_text(encoding="utf-8")
                except OSError as exc:
                    ui_console.print_error(f"Cannot read {p}: {exc}")
                    continue
                try:
                    imported_messages = parse_import(raw_text)
                except ValueError as exc:
                    ui_console.print_error(f"Invalid conversation file: {exc}")
                    continue
                # Validation passed: only now mutate state (fail-safe — any
                # failure above already continued with zero changes).
                messages[:] = imported_messages
                token_tracker.reset()
                new_sid = _generate_session_id(
                    transcript_mgr,
                    reserved_ids={_get_compact_session_id(compact_fn)},
                )
                _set_compact_session_id(compact_fn, new_sid)
                _set_interaction_logger_session(interaction_logger, new_sid)
                message_bus, main_mailbox_cursor = _switch_session_mailbox(
                    workspace_path,
                    new_sid,
                    current_bus=message_bus,
                )
                spawned_agents = {}
                pending_team_messages.clear()
                subagent_registry.clear()
                handlers = _build_handlers(
                    workspace_path=workspace_path,
                    todo_manager=todo_manager,
                    task_manager=task_manager,
                    skill_loader=skill_loader,
                    provider=provider,
                    tools=tools,
                    permission=permission,
                    bg_manager=bg_manager,
                    messages=messages,
                    config=config,
                    runtime_id=new_sid,
                    teammate_manager=teammate_manager,
                    message_bus=message_bus,
                    spawned_agents=spawned_agents,
                    agent_name=MAIN_AGENT_NAME,
                    mcp_manager=mcp_manager,
                    lsp_manager=lsp_manager,
                    memory_manager=memory_manager,
                    subagent_registry=subagent_registry,
                )
                _install_plan_handler(handlers, plan_approval)
                install_workflow_handler(handlers)
                install_subagent_send_handler(handlers)
                _replay_stdio_transcript(messages, ui_console)
                _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                ui_console.print_status(
                    f"Imported {len(messages)} messages into new session: {new_sid}"
                )
                continue
            if text == "/cost":
                ui_console.print_status(token_tracker.summary(config.cost.prices))
                continue
            if text == "/goal" or text.startswith("/goal "):
                goal_cmd = parse_goal_command(
                    text[len("/goal") :], default_max_turns=config.goal.max_turns
                )
                if goal_cmd.action == "run":
                    _drive_goal(
                        goal_cmd,
                        provider=provider,
                        evaluator_provider=goal_evaluator_provider,
                        messages=messages,
                        loop_tools=loop_tools,
                        handlers=handlers,
                        permission=permission,
                        compact_fn=compact_fn,
                        bg_manager=bg_manager,
                        config=config,
                        ui_console=ui_console,
                        interaction_logger=interaction_logger,
                        token_tracker=token_tracker,
                        hook_engine=hook_engine,
                        retry_policy=retry_policy,
                        transcript_mgr=transcript_mgr,
                    )
                elif goal_cmd.action == "error":
                    ui_console.print_error(goal_cmd.message)
                else:  # usage
                    ui_console.print_status(goal_cmd.message)
                continue
            if text == "/loop" or text.startswith("/loop "):
                _dispatch_loop_command(text, scheduler=scheduler, ui_console=ui_console)
                continue
            if text == "/log" or text.startswith("/log "):
                viewer_server = _handle_log_command(
                    text,
                    config=config,
                    interaction_logger=interaction_logger,
                    viewer_server=viewer_server,
                    print_status=ui_console.print_status,
                )
                continue
            if text in _PERMISSION_SLASH:
                old = permission.mode
                permission.mode = _PERMISSION_SLASH[text]
                ui_console.print_status(f"Permission mode: {old.value} → {permission.mode.value}")
                continue
            if text == "/mode":
                _handle_mode_selection_stdio(permission, ui_console)
                continue
            if text == "/theme" or text.startswith("/theme "):
                from src.ui.theme import (
                    format_theme_list,
                    format_unknown_theme,
                    get_theme,
                )

                _, _, theme_arg = text.partition(" ")
                theme_name = theme_arg.strip()
                tm = get_theme()
                if not theme_name:
                    ui_console.print_status(format_theme_list(tm))
                    continue
                if tm.switch(theme_name):
                    ui_console.set_theme(tm)
                    ui_console.print_status(f"Theme switched to: {theme_name}")
                    continue
                ui_console.print_error(format_unknown_theme(theme_name))
                continue
            if text == "/team" or text.startswith("/team "):
                _handle_team_command(
                    text,
                    ui_console,
                    teammate_manager=teammate_manager,
                    team_handlers=handlers,
                )
                continue
            if text == "/mcp" or (text.startswith("/mcp ") and not text.startswith("/mcp:")):
                _dispatch_mcp_command(
                    text,
                    mcp_manager=mcp_manager,
                    ui_console=ui_console,
                )
                continue
            if text == "/lsp" or text.startswith("/lsp "):
                _dispatch_lsp_command(
                    text,
                    lsp_manager=lsp_manager,
                    ui_console=ui_console,
                )
                continue
            if text == "/skill" or text.startswith("/skill "):
                _dispatch_skill_command(
                    text,
                    store=skill_store,
                    loader=skill_loader,
                    console=ui_console,
                )
                continue
            if text == "/reload":
                _dispatch_reload_command(
                    config=config,
                    permission=permission,
                    ui_console=ui_console,
                )
                last_config_mtimes = _config_mtimes(config)
                continue
            if text.startswith("/mcp:"):
                snapshot_len = len(messages)
                appended = _dispatch_mcp_prompt(
                    text,
                    mcp_manager=mcp_manager,
                    messages=messages,
                    ui_console=ui_console,
                )
                if not appended:
                    continue
                # Re-render the injected user turn(s) so the screen matches the
                # transcript before agent_loop runs.
                _replay_stdio_transcript(messages[snapshot_len:], ui_console)
                if messages[-1].get("role") != "user":
                    # Trailing assistant message — no LLM call needed; prompt for input.
                    ui_console.print_status("Prompt injected. Type your follow-up to continue.")
                    continue
                try:
                    agent_loop(
                        provider=provider,
                        messages=messages,
                        tools=loop_tools,
                        handlers=handlers,
                        permission=permission,
                        compact_fn=compact_fn,
                        bg_manager=bg_manager,
                        stream=config.ui.stream,
                        console=ui_console,
                        interaction_logger=interaction_logger,
                        token_tracker=token_tracker,
                        hook_engine=hook_engine,
                        retry_policy=retry_policy,
                    )
                    _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                    main_mailbox_cursor = _drain_team_mailbox(
                        ui_console,
                        message_bus=message_bus,
                        since=main_mailbox_cursor,
                        sink=pending_team_messages,
                    )
                except LLMCallError:
                    del messages[snapshot_len:]
                    ui_console.print_error("LLM call failed, please try again.")
                except KeyboardInterrupt:
                    del messages[snapshot_len:]
                    ui_console.print_status("Agent loop interrupted.")
                continue
            if text == "/remember" or text.startswith("/remember "):
                if memory_manager is None:
                    ui_console.print_error(
                        "Persistent memory is disabled (enable [memory] in config)."
                    )
                    continue
                _, _, remember_arg = text.partition(" ")
                # Rewrite into an LLM instruction and fall through to the
                # normal user-turn handling below, which runs agent_loop.
                text = build_remember_instruction(remember_arg.strip())
            elif text == "/forget" or text.startswith("/forget "):
                if memory_manager is None:
                    ui_console.print_error(
                        "Persistent memory is disabled (enable [memory] in config)."
                    )
                    continue
                _, _, forget_arg = text.partition(" ")
                text = build_forget_instruction(forget_arg.strip())

            # Prepend any buffered late/unsolicited teammate replies onto this
            # user turn so the LLM sees them (without injecting standalone user
            # messages that would break role alternation).
            if pending_team_messages:
                team_context = "\n".join(pending_team_messages)
                pending_team_messages.clear()
                text = f"{team_context}\n\n{text}" if text else team_context

            messages.append({"role": "user", "content": text})
            snapshot_len = len(messages) - 1
            try:
                agent_loop(
                    provider=provider,
                    messages=messages,
                    tools=loop_tools,
                    handlers=handlers,
                    permission=permission,
                    compact_fn=compact_fn,
                    bg_manager=bg_manager,
                    stream=config.ui.stream,
                    console=ui_console,
                    interaction_logger=interaction_logger,
                    token_tracker=token_tracker,
                    hook_engine=hook_engine,
                    retry_policy=retry_policy,
                    skill_gen=skill_generator,
                )
                _save_transcript_snapshot(transcript_mgr, messages, compact_fn)
                # Experiential skill generation: when this turn pushed the
                # cumulative activity past both thresholds, reflect on the
                # session and draft a reusable skill (isolated extra LLM call).
                # Reset first so the trigger does not re-fire every later turn.
                if skill_generator is not None and skill_generator.should_draft():
                    skill_generator.reset()
                    _run_skill_reflection(
                        provider=provider,
                        messages=messages,
                        store=skill_store,
                        skill_loader=skill_loader,
                        console=ui_console,
                        token_tracker=token_tracker,
                        permission=permission,
                        max_pending=config.skills.max_pending,
                    )
                main_mailbox_cursor = _drain_team_mailbox(
                    ui_console,
                    message_bus=message_bus,
                    since=main_mailbox_cursor,
                    sink=pending_team_messages,
                )
            except LLMCallError:
                del messages[snapshot_len:]
                ui_console.print_error("LLM call failed, please try again.")
            except KeyboardInterrupt:
                del messages[snapshot_len:]
                ui_console.print_status("Agent loop interrupted.")
    finally:
        try:
            scheduler.cancel_all()
        except Exception:
            pass
        try:
            mcp_manager.close_all()
        except Exception:
            pass
        try:
            lsp_manager.close_all()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = resolve_config_path(args.config)

    if getattr(args, "command", None) == "init":
        return 0 if run_setup_wizard(config_path=config_path) else 1

    try:
        config = load_config(
            config_path,
            provider_override=args.provider,
            model_override=args.model,
        )
    except FileNotFoundError:
        print(f"Config file not found: {config_path}")
        return 1
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Failed to load config: {exc}")
        return 1

    # First-run convenience: when no usable API key is configured and we are on
    # an interactive terminal, drop into the same setup wizard rather than
    # failing later in ``create_provider``. Non-TTY runs keep the existing
    # fail-fast behaviour below.
    provider_config = getattr(config, "provider", None)
    if (
        isinstance(provider_config, ProviderConfig)
        and not _has_usable_key(provider_config)
        and sys.stdin.isatty()
    ):
        print("No usable API key detected. Entering interactive setup...")
        if run_setup_wizard(config_path=config_path):
            try:
                config = load_config(
                    config_path,
                    provider_override=args.provider,
                    model_override=args.model,
                )
            except (FileNotFoundError, tomllib.TOMLDecodeError, ValueError) as exc:
                print(f"Failed to reload config after setup: {exc}")
                return 1

    try:
        provider = create_provider(config)
    except ValueError as exc:
        print(f"Failed to initialize provider: {exc}")
        return 1

    return _run_stdio_session(config, provider)


if __name__ == "__main__":
    raise SystemExit(main())
