from __future__ import annotations

import json
import re
import shlex
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
_SHELL_COMMAND_TOOLS = {"bash", "background_run"}
_ALWAYS_MUTATING_TOOLS = {
    "edit_file",
    "write_file",
    "semantic_rename",
    "task_create",
    "task_update",
}
_MEMORY_READ_COMMANDS = {"view"}

_GIT_EXECUTABLES = {"git", "git.exe"}
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-c",
    "--attr-source",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--list-cmds",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
_GIT_READ_ONLY_SUBCOMMANDS = {"diff", "log", "show", "status"}
_BRANCH_LISTING_OPTIONS = {
    "-a",
    "-l",
    "-r",
    "--all",
    "--contains",
    "--format",
    "--ignore-case",
    "--list",
    "--merged",
    "--no-contains",
    "--no-color",
    "--no-column",
    "--no-merged",
    "--omit-empty",
    "--points-at",
    "--remotes",
    "--show-current",
    "--sort",
    "--verbose",
}
_BRANCH_MUTATING_OPTIONS = {
    "--copy",
    "--delete",
    "--edit-description",
    "--force",
    "--move",
    "--no-track",
    "--recurse-submodules",
    "--set-upstream-to",
    "--track",
    "--unset-upstream",
}

_MCP_TOOL_PREFIX = "mcp__"
# Preview limits for MCP ask prompts. MCP args are JSON, not shell text, and
# servers can produce arbitrarily large strings (file blobs, long URLs). Cap
# top-level string values so a single field can't flood the terminal.
_MCP_PREVIEW_FIELD_LIMIT = 256


def _is_mcp_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` follows the ``mcp__<server>__<tool>`` namespace."""
    return tool_name.startswith(_MCP_TOOL_PREFIX)


def _shell_tokens(command: str) -> list[str] | None:
    """Split shell text while preserving Windows paths and command separators."""
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _strip_shell_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def _is_shell_separator(token: str) -> bool:
    return bool(token) and all(character in ";&|" for character in token)


def _is_git_executable(token: str) -> bool:
    normalized = _strip_shell_quotes(token).replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].casefold() in _GIT_EXECUTABLES


def _parse_git_invocation(tokens: list[str], executable_index: int) -> tuple[str, list[str]] | None:
    end = executable_index + 1
    while end < len(tokens) and not _is_shell_separator(tokens[end]):
        end += 1

    index = executable_index + 1
    while index < end:
        token = _strip_shell_quotes(tokens[index])
        normalized = token.casefold()
        if normalized == "--":
            index += 1
            break
        if normalized in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if normalized.startswith("-"):
            index += 1
            continue
        arguments = [_strip_shell_quotes(argument) for argument in tokens[index + 1 : end]]
        return normalized, arguments

    if index < end:
        subcommand = _strip_shell_quotes(tokens[index]).casefold()
        arguments = [_strip_shell_quotes(argument) for argument in tokens[index + 1 : end]]
        return subcommand, arguments
    return None


def _git_invocations(command: str) -> list[tuple[str, list[str]]]:
    tokens = _shell_tokens(command)
    if tokens is None:
        return []
    invocations: list[tuple[str, list[str]]] = []
    for index, token in enumerate(tokens):
        if not _is_git_executable(token):
            continue
        invocation = _parse_git_invocation(tokens, index)
        if invocation is not None:
            invocations.append(invocation)
    return invocations


def _has_force_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        if normalized == "--force" or normalized.startswith(
            ("--force=", "--force-if-includes", "--force-with-lease")
        ):
            return True
        if re.fullmatch(r"-[a-z]*f[a-z]*", normalized):
            return True
    return False


def _has_branch_delete_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        if normalized == "--delete" or normalized.startswith("--delete="):
            return True
        if re.fullmatch(r"-[a-z]*d[a-z]*", normalized):
            return True
    return False


def _is_dangerous_git_command(command: str) -> bool:
    for subcommand, arguments in _git_invocations(command):
        if subcommand in {"clean", "push"} and _has_force_option(arguments):
            return True
        if subcommand == "reset" and any(argument.casefold() == "--hard" for argument in arguments):
            return True
        if subcommand == "branch" and _has_branch_delete_option(arguments):
            return True
    return False


def _has_output_option(arguments: list[str]) -> bool:
    return any(
        argument.casefold() == "--output" or argument.casefold().startswith("--output=")
        for argument in arguments
    )


def _has_mutating_branch_option(argument: str) -> bool:
    normalized = argument.casefold()
    if normalized in _BRANCH_MUTATING_OPTIONS:
        return True
    if any(normalized.startswith(f"{option}=") for option in _BRANCH_MUTATING_OPTIONS):
        return True
    return re.fullmatch(r"-[a-z]*[dmcfut][a-z]*", normalized) is not None


def _is_read_only_branch(arguments: list[str]) -> bool:
    if not arguments:
        return True
    if any(_has_mutating_branch_option(argument) for argument in arguments):
        return False
    if all(argument.startswith("-") for argument in arguments):
        return True
    return any(
        normalized in _BRANCH_LISTING_OPTIONS
        or any(normalized.startswith(f"{option}=") for option in _BRANCH_LISTING_OPTIONS)
        for normalized in (argument.casefold() for argument in arguments)
    )


def _is_read_only_git_command(command: str) -> bool:
    tokens = _shell_tokens(command)
    if tokens is None or not tokens:
        return False

    executable_index = 0
    if tokens[0] == "&":
        executable_index = 1
    if executable_index >= len(tokens) or not _is_git_executable(tokens[executable_index]):
        return False

    invocation = _parse_git_invocation(tokens, executable_index)
    if invocation is None:
        return False
    subcommand, arguments = invocation
    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return not _has_output_option(arguments)
    if subcommand == "branch":
        return _is_read_only_branch(arguments)
    return False


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
        re.compile(r"^(pytest|python\s+-m\s+pytest|ruff|mypy)\b"),
        re.compile(r"^npm\s+(test|run\s+lint|run\s+test)\b"),
    ]
    # PowerShell command and executable resolution is case-insensitive on the
    # Windows execution path. Compile the complete set consistently so casing
    # the invoked command cannot turn destructive input into an AUTO bypass.
    DANGEROUS_PATTERNS = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"(^|[\s;&|])rm\s+-[a-z]*r[a-z]*\b",
            r"\bremove-item\b(?=[^;\r\n|]*\s-recurse\b)(?=[^;\r\n|]*\s-force\b)",
            r"\bgit\s+push\b[^\r\n;&|]*(?<!\S)(?:--force(?:-with-lease|-if-includes)?|-[a-z]*f[a-z]*)\b",
            r"\bgit\s+clean\b[^\r\n;&|]*(?<!\S)(?:--force\b|-[a-z]*f[a-z]*\b)",
            r"git\s+reset\s+--hard\b",
            r"DROP\s+TABLE\b",
            r"DELETE\s+FROM\b",
            # shell wrapper bypass
            rf"(^|\s)({_SHELLS})\s+-c\b",
            # absolute-path rm bypass
            r"(^|\s)/(?:usr/)?bin/rm\b",
            # env prefix bypass
            r"(^|\s)env\s+",
            # pipe-to-shell execution
            rf"curl\b.*\|\s*({_SHELLS})\b",
            rf"wget\b.*\|\s*({_SHELLS})\b",
            # destructive system commands
            r"(^|\s)chmod\s+777\b",
            r"(^|\s)mkfs\b",
            r"(^|\s)dd\s+if=",
            r"find\b.*-delete\b",
        )
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
        if _is_mutating_tool(normalized_tool, tool_input):
            return True
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
        if normalized_tool in _SHELL_COMMAND_TOOLS:
            cmd = rule_subject or ""
            if self._match_rules(self.deny_rules, normalized_tool, cmd):
                return True
            if _is_dangerous_git_command(cmd) or any(
                pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS
            ):
                return True
            if self._match_rules(self.allow_rules, normalized_tool, cmd):
                return False
            if _is_read_only_git_command(cmd) or any(
                pattern.search(cmd) for pattern in self.AUTO_SAFE_PATTERNS
            ):
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
        if rule_subject and self._match_rules(
            self.allow_rules,
            normalized_tool,
            rule_subject,
        ):
            return False
        if normalized_tool in self.SAFE_TOOLS:
            return False
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
        if normalized_tool in _SHELL_COMMAND_TOOLS:
            cmd = str(tool_input.get("command", ""))
            return _is_dangerous_git_command(cmd) or any(
                pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS
            )
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


def _is_mutating_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name in _ALWAYS_MUTATING_TOOLS:
        return True
    if tool_name != "memory":
        return False
    command = str(tool_input.get("command", "")).strip().lower()
    return command not in _MEMORY_READ_COMMANDS


def permission_rule_subject(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    normalized_tool = tool_name.strip().lower()
    if normalized_tool in _SHELL_COMMAND_TOOLS:
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
