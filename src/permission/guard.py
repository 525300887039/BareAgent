from __future__ import annotations

import json
import re
import sys
from enum import Enum
from typing import Any


class PermissionMode(Enum):
    DEFAULT = "default"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


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
        re.compile(r"(^|\s)(bash|sh|zsh|dash|ksh|fish)\s+-c\b"),
        # absolute-path rm bypass
        re.compile(r"(^|\s)/(?:usr/)?bin/rm\b"),
        # env prefix bypass
        re.compile(r"(^|\s)env\s+"),
        # pipe-to-shell execution
        re.compile(r"curl\b.*\|\s*(bash|sh|zsh|dash|ksh|fish)\b"),
        re.compile(r"wget\b.*\|\s*(bash|sh|zsh|dash|ksh|fish)\b"),
        # destructive system commands
        re.compile(r"(^|\s)chmod\s+777\b"),
        re.compile(r"(^|\s)mkfs\b"),
        re.compile(r"(^|\s)dd\s+if="),
        re.compile(r"find\b.*-delete\b"),
    ]

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self.mode = mode
        self.allow_rules: list[str] = []
        self.deny_rules: list[str] = []

    def requires_confirm(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if self.mode == PermissionMode.BYPASS:
            return False
        if self.mode == PermissionMode.PLAN:
            return tool_name not in self.SAFE_TOOLS
        if tool_name in self.SAFE_TOOLS:
            return False
        if tool_name in {"edit_file", "task_create", "task_update"}:
            return False
        if tool_name == "write_file":
            return self.mode == PermissionMode.DEFAULT
        if tool_name != "bash":
            return True

        cmd = str(tool_input.get("command", "")).strip()
        if self._match_rules(self.deny_rules, tool_name, cmd):
            return True
        if any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS):
            return True
        if self._match_rules(self.allow_rules, tool_name, cmd):
            return False
        if any(pattern.search(cmd) for pattern in self.AUTO_SAFE_PATTERNS):
            return False
        if self.mode == PermissionMode.DEFAULT:
            return True
        # AUTO mode: not matching any dangerous pattern, allow
        return False

    def ask_user(self, call: Any) -> bool:
        if self.mode == PermissionMode.PLAN:
            print(f"Plan mode: {call.name} blocked (read-only)")
            return False
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


def _parse_prefix_rule(rule: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\(prefix:(.+)\)\s*", rule)
    if match is None:
        return None
    tool_name = match.group(1).strip().lower()
    prefix = match.group(2).rstrip("*").strip()
    return tool_name, prefix
