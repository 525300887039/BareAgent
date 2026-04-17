from __future__ import annotations

import json
import re
import sys
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.planning.agent_types import AgentType


class PermissionMode(Enum):
    DEFAULT = "default"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


_SHELLS = "bash|sh|zsh|dash|ksh|fish"


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
        if normalized_tool == "write_file":
            return self.mode == PermissionMode.DEFAULT
        return True

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
        agent_type: "AgentType",
        *,
        background: bool = False,
    ) -> PermissionGuard:
        """Clone the guard for child-agent execution."""
        resolved_mode = (
            agent_type.permission_mode
            if agent_type.permission_mode is not None
            else self.mode
        )
        return self.clone(
            mode=resolved_mode,
            fail_closed=self.fail_closed
            or background
            or resolved_mode == PermissionMode.PLAN,
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
