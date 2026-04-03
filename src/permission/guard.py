from __future__ import annotations

import json
import re
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
        if tool_name in {"write_file", "edit_file"}:
            return False
        if tool_name != "bash":
            return True

        cmd = str(tool_input.get("command", "")).strip()
        if self._match_rules(self.deny_rules, tool_name, cmd):
            return True
        if self._match_rules(self.allow_rules, tool_name, cmd):
            return False
        if self.mode == PermissionMode.AUTO:
            if any(pattern.search(cmd) for pattern in self.AUTO_SAFE_PATTERNS):
                return False
        return any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS)

    def ask_user(self, call: Any) -> bool:
        if self.mode == PermissionMode.PLAN:
            print(f"Plan mode: {call.name} blocked (read-only)")
            return False
        print(f"{call.name}: {json.dumps(call.input, ensure_ascii=False)[:200]}")
        return input("Allow? [y/N] ").strip().lower() == "y"

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
