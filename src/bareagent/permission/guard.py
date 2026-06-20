from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bareagent.planning.agent_types import AgentType


class PermissionMode(Enum):
    DEFAULT = "default"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


_SHELLS = "bash|sh|zsh|dash|ksh|fish"

_MCP_TOOL_PREFIX = "mcp__"
# Preview limits for MCP ask prompts. MCP args are JSON, not shell text, and
# servers can produce arbitrarily large strings (file blobs, long URLs). Cap
# top-level string values so a single field can't flood the terminal.
_MCP_PREVIEW_FIELD_LIMIT = 256


def _is_mcp_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` follows the ``mcp__<server>__<tool>`` namespace."""
    return tool_name.startswith(_MCP_TOOL_PREFIX)


class PermissionGuard:
    SAFE_TOOLS = {
        "read_file",
        "glob",
        "grep",
        "todo_read",
        "todo_write",
        "load_skill",
        "task_list",
        "task_get",
        "team_list",
        "web_fetch",
        "web_search",
        # code_search is read-only semantic retrieval (like grep): it embeds and
        # ranks files but never mutates anything, so prompting would be noise.
        "code_search",
        # repo_map is read-only structural retrieval (tree-sitter symbol skeleton
        # ranked by PageRank): like code_search/grep it only reads, no prompt.
        "repo_map",
        # Memory is sandboxed to its own directory (never user code) and is
        # agent bookkeeping; prompting on every recall/save would be noise.
        # Read-only isolation for sub-agents is handled at the AgentType layer
        # (memory_writable), not here.
        "memory",
        # skill_create writes only to the generated-skills pending sandbox and
        # is exposed only inside the isolated reflection call (never the main
        # tool set / sub-agents), so prompting would be noise.
        "skill_create",
        # goal_verdict only records the evaluator's judgement into an in-memory
        # sink (no workspace side effects) and is exposed only inside the
        # isolated goal-evaluator call (never the main tool set / sub-agents),
        # so prompting would be noise.
        "goal_verdict",
        # exit_plan_mode is the *only* way out of PLAN mode; it MUST be allowed
        # while in PLAN (a non-SAFE tool is blocked there). Its own action is the
        # approval prompt, so a separate permission confirm would be redundant.
        # It is a main-loop-only tool (never in the global set / sub-agents).
        "exit_plan_mode",
    }
    AUTO_SAFE_PATTERNS = [
        re.compile(r"^(ls|cat|head|tail|wc|echo|pwd|date|which|type)\b"),
        re.compile(r"^git\s+(status|log|diff|branch|show)\b"),
        re.compile(r"^(pytest|python\s+-m\s+pytest|ruff|mypy)\b"),
        re.compile(r"^npm\s+(test|run\s+lint|run\s+test)\b"),
    ]
    DANGEROUS_PATTERNS = [
        re.compile(r"(^|\s)rm\s+-[rR]f?\b"),
        re.compile(r"git\s+push\s+--force\b"),
        re.compile(r"git\s+reset\s+--hard\b"),
        re.compile(r"DROP\s+TABLE\b", re.IGNORECASE),
        re.compile(r"DELETE\s+FROM\b", re.IGNORECASE),
        # shell wrapper bypass
        re.compile(rf"(^|\s)({_SHELLS})\s+-c\b"),
        # absolute-path rm bypass
        re.compile(r"(^|\s)/(?:usr/)?bin/rm\b"),
        # env prefix bypass
        re.compile(r"(^|\s)env\s+"),
        # pipe-to-shell execution
        re.compile(rf"curl\b.*\|\s*({_SHELLS})\b"),
        re.compile(rf"wget\b.*\|\s*({_SHELLS})\b"),
        # destructive system commands
        re.compile(r"(^|\s)chmod\s+777\b"),
        re.compile(r"(^|\s)mkfs\b"),
        re.compile(r"(^|\s)dd\s+if="),
        re.compile(r"find\b.*-delete\b"),
    ]

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        *,
        fail_closed: bool = False,
        ask_user_fn: Callable[[Any], bool] | None = None,
    ) -> None:
        self.mode = mode
        self.allow_rules: list[str] = []
        self.deny_rules: list[str] = []
        self.fail_closed = fail_closed
        self._ask_user_fn = ask_user_fn

    def requires_confirm(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if self.mode == PermissionMode.BYPASS:
            return False
        normalized_tool = tool_name.strip().lower()
        rule_subject = permission_rule_subject(normalized_tool, tool_input)
        # MCP tools carry JSON args (not shell text), so DANGEROUS_PATTERNS
        # are not applicable. Branch early on mode but still honour the
        # generic allow/deny prefix rules (handled below via rule_subject).
        if _is_mcp_tool(normalized_tool):
            # PLAN mode rejects every MCP tool by policy — MCP servers have
            # unknown side effects and are not in SAFE_TOOLS. This check runs
            # before allow_rules so an allowlist in config.toml cannot punch
            # holes through PLAN.
            if self.mode == PermissionMode.PLAN:
                return True
            if rule_subject and self._match_rules(
                self.deny_rules,
                normalized_tool,
                rule_subject,
            ):
                return True
            if rule_subject and self._match_rules(
                self.allow_rules,
                normalized_tool,
                rule_subject,
            ):
                return False
            if self.mode == PermissionMode.AUTO:
                return False
            # DEFAULT: always ask for MCP tools.
            return True
        if self.mode == PermissionMode.PLAN:
            return normalized_tool not in self.SAFE_TOOLS
        if normalized_tool == "bash":
            cmd = rule_subject or ""
            if self._match_rules(self.deny_rules, normalized_tool, cmd):
                return True
            if any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS):
                return True
            if self._match_rules(self.allow_rules, normalized_tool, cmd):
                return False
            if any(pattern.search(cmd) for pattern in self.AUTO_SAFE_PATTERNS):
                return False
            if self.mode == PermissionMode.DEFAULT:
                return True
            # AUTO mode: not matching any dangerous pattern, allow
            return False

        if rule_subject and self._match_rules(
            self.deny_rules,
            normalized_tool,
            rule_subject,
        ):
            return True
        if normalized_tool in self.SAFE_TOOLS:
            return False
        if normalized_tool in {"edit_file", "task_create", "task_update"}:
            return False
        if rule_subject and self._match_rules(
            self.allow_rules,
            normalized_tool,
            rule_subject,
        ):
            return False
        if normalized_tool in {"write_file", "semantic_rename"}:
            # Write tools: confirm in DEFAULT, auto-approve in AUTO. PLAN was
            # already rejected above (not in SAFE_TOOLS), BYPASS short-circuited
            # at the top.
            return self.mode == PermissionMode.DEFAULT
        return True

    def is_dangerous(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Return True if ``tool_name`` + ``tool_input`` match a known dangerous shell pattern.

        DANGEROUS_PATTERNS encode shell-text heuristics (``rm -rf``,
        ``git push --force``, ``DROP TABLE``...). They are intentionally
        skipped for MCP tools, whose ``tool_input`` is JSON rather than a
        shell command — applying shell regexes against JSON would produce
        false positives without catching anything real.
        """
        normalized_tool = tool_name.strip().lower()
        if _is_mcp_tool(normalized_tool):
            return False
        if normalized_tool == "bash":
            cmd = str(tool_input.get("command", ""))
            return any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS)
        return False

    def format_preview(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Return a human-readable JSON preview of ``tool_input`` for ask prompts.

        Top-level string values longer than ``_MCP_PREVIEW_FIELD_LIMIT`` are
        truncated with a ``... [truncated, N chars]`` suffix so a single huge
        argument (file blob, long URL) cannot drown the terminal. Nested
        structures are not recursively truncated — v1 keeps the rule simple.
        """
        if not isinstance(tool_input, dict) or not tool_input:
            return json.dumps(tool_input, ensure_ascii=False, indent=2)
        prepared: dict[str, Any] = {}
        for key, value in tool_input.items():
            if isinstance(value, str) and len(value) > _MCP_PREVIEW_FIELD_LIMIT:
                prepared[key] = (
                    value[:_MCP_PREVIEW_FIELD_LIMIT] + f"... [truncated, {len(value)} chars]"
                )
            else:
                prepared[key] = value
        return json.dumps(prepared, ensure_ascii=False, indent=2, default=str)

    def ask_user(self, call: Any) -> bool:
        if self.fail_closed:
            return False
        if self.mode == PermissionMode.PLAN:
            print(f"Plan mode: {call.name} blocked (read-only)")
            return False
        if self._ask_user_fn is not None:
            return self._ask_user_fn(call)
        if not sys.stdin.isatty():
            print(f"Non-interactive environment: {call.name} denied")
            return False
        print(f"{call.name}: {json.dumps(call.input, ensure_ascii=False)[:200]}")
        try:
            return input("Allow? [y/N] ").strip().lower() == "y"
        except EOFError:
            return False

    def _match_rules(self, rules: list[str], tool_name: str, cmd: str) -> bool:
        normalized_tool = tool_name.strip().lower()
        for rule in rules:
            parsed = _parse_prefix_rule(rule)
            if parsed is None:
                continue
            rule_tool, prefix = parsed
            if rule_tool != normalized_tool:
                continue
            if cmd.strip().startswith(prefix):
                return True
        return False

    def clone(
        self, *, mode: PermissionMode | None = None, fail_closed: bool | None = None
    ) -> PermissionGuard:
        """Create a copy of this guard with optional overrides."""
        child = PermissionGuard(
            mode=mode if mode is not None else self.mode,
            fail_closed=fail_closed if fail_closed is not None else self.fail_closed,
            ask_user_fn=self._ask_user_fn,
        )
        child.allow_rules = list(self.allow_rules)
        child.deny_rules = list(self.deny_rules)
        return child

    def for_subagent(
        self,
        agent_type: AgentType,
        *,
        background: bool = False,
    ) -> PermissionGuard:
        """Clone the guard for child-agent execution."""
        resolved_mode = (
            agent_type.permission_mode if agent_type.permission_mode is not None else self.mode
        )
        return self.clone(
            mode=resolved_mode,
            fail_closed=self.fail_closed or background or resolved_mode == PermissionMode.PLAN,
        )


def _parse_prefix_rule(rule: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\((prefix|prefix_json):([\s\S]+)\)\s*",
        rule,
    )
    if match is None:
        return None
    tool_name = match.group(1).strip().lower()
    rule_kind = match.group(2)
    raw_prefix = match.group(3)
    if rule_kind == "prefix_json":
        try:
            parsed_prefix = json.loads(raw_prefix)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed_prefix, str):
            return None
        return tool_name, parsed_prefix
    prefix = raw_prefix.rstrip("*").strip()
    return tool_name, prefix


def permission_rule_subject(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    normalized_tool = tool_name.strip().lower()
    if normalized_tool == "bash":
        command = str(tool_input.get("command", "")).strip()
        return command or None

    for key in ("file_path", "path", "name", "to_agent", "task_id", "skill_name"):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        subject = value.strip()
        if subject:
            return subject

    if "task" in tool_input:
        task = str(tool_input.get("task", "")).strip()
        if task:
            return task

    if not tool_input:
        return None

    try:
        serialized = json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        serialized = str(tool_input).strip()
    return serialized or None
