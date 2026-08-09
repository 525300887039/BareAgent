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
_SHELL_WHITESPACE = " \t"
_SHELL_CONTROL_CHARACTERS = ";&|<>()\r\n"
_SHELL_COMMAND_SEPARATOR_CHARACTERS = ";&|(){}\r\n"
_SHELL_COMMAND_TOOLS = {"bash", "background_run"}
_TRANSPARENT_SHELL_PREFIXES = {"command", "exec", "nohup", "sudo"}
_SUDO_OPTIONS_WITH_VALUE = {
    "-C",
    "--close-from",
    "-D",
    "--chdir",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-R",
    "--chroot",
    "-r",
    "--role",
    "-T",
    "--command-timeout",
    "-t",
    "--type",
    "-u",
    "--user",
}
_SUDO_COMMAND_FLAGS = {
    "-A",
    "--askpass",
    "-b",
    "--background",
    "-E",
    "--preserve-env",
    "-H",
    "--set-home",
    "-k",
    "--reset-timestamp",
    "-n",
    "--non-interactive",
    "-P",
    "--preserve-groups",
    "-S",
    "--stdin",
    "-i",
    "--login",
    "-s",
    "--shell",
}
_ALWAYS_MUTATING_TOOLS = {
    "edit_file",
    "write_file",
    "semantic_rename",
    "task_create",
    "task_update",
}
_MEMORY_READ_COMMANDS = {"view"}

_GIT_EXECUTABLES = {"git", "git.exe"}
_RM_EXECUTABLES = {"rm", "rm.exe"}
_POWERSHELL_REMOVE_ITEM_EXECUTABLES = {
    "remove-item",
    "del",
    "erase",
    "rd",
    "rmdir",
    "ri",
}
_POWERSHELL_RECURSE_OPTIONS = {
    "-r",
    "-re",
    "-rec",
    "-recu",
    "-recur",
    "-recurs",
    "-recurse",
}
# GNU rm accepts unique prefixes of long options, including --r/--rec.
_RM_RECURSIVE_LONG_OPTION_PREFIXES = {
    "--r",
    "--re",
    "--rec",
    "--recu",
    "--recur",
    "--recurs",
    "--recursive",
}
_CHMOD_EXECUTABLES = {"chmod", "chmod.exe"}
_POSIX_SHELL_EXECUTABLES = {
    executable for shell in _SHELLS.split("|") for executable in (shell, f"{shell}.exe")
}
_POWERSHELL_EXECUTABLES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_POWERSHELL_EVAL_EXECUTABLES = {"invoke-expression", "iex"}
_CMD_EXECUTABLES = {"cmd", "cmd.exe"}
_ENV_EXECUTABLES = {"env", "env.exe"}
_POSIX_SHELL_OPTIONS_WITH_VALUE = {"-o", "--init-file", "--rcfile"}
_POWERSHELL_51_SPECIAL_ESCAPES = "0abfnrtv\"'`{} \t"
_BASH_ANSI_ESCAPE_PATTERN = re.compile(
    r"\\(?:[abefnrtvE\\'\"]|x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|"
    r"U[0-9a-fA-F]{1,8}|[0-7]{1,3}|\r?\n)"
)
_BASH_ANSI_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
}
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


def _tokenize_shell(
    command: str, *, posix: bool, punctuation_chars: str = _SHELL_CONTROL_CHARACTERS
) -> list[str] | None:
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=punctuation_chars)
    lexer.whitespace = _SHELL_WHITESPACE
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _shell_tokens(command: str) -> list[str] | None:
    """Split shell text while preserving Windows paths and command separators."""
    return _tokenize_shell(command, posix=False)


def _normalize_shell_escapes(
    command: str, *, escape_character: str, special_escapes: str | None = None
) -> str:
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote != "'" and character == escape_character and index + 1 < len(command):
            next_character = command[index + 1]
            if next_character == "\n":
                index += 2
                continue
            if next_character == "\r" and command[index + 2 : index + 3] == "\n":
                index += 3
                continue
            if special_escapes is None:
                normalized.extend((character, next_character))
            elif next_character in special_escapes:
                normalized.append("\ufffd")
            else:
                normalized.append(next_character)
            index += 2
            continue
        normalized.append(character)
        if character == quote:
            quote = None
        elif quote is None and character in {'"', "'"}:
            quote = character
        index += 1
    return "".join(normalized)


def _decode_bash_ansi_c(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        escape = match.group()[1:]
        if escape in _BASH_ANSI_SIMPLE_ESCAPES:
            return _BASH_ANSI_SIMPLE_ESCAPES[escape]
        if escape.endswith("\n"):
            return ""
        base = 16 if escape[0] in "xXuU" else 8
        digits = escape[1:] if base == 16 else escape
        codepoint = int(digits, base)
        return chr(codepoint) if codepoint <= 0x10FFFF else "\ufffd"

    return _BASH_ANSI_ESCAPE_PATTERN.sub(replace, value)


def _quote_posix_token(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _normalize_bash_quotes(command: str) -> str:
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote is not None:
            normalized.append(character)
            if quote == '"' and character == "\\" and index + 1 < len(command):
                normalized.append(command[index + 1])
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            normalized.extend((character, command[index + 1]))
            index += 2
            continue
        if character == "$" and command[index + 1 : index + 2] == "'":
            end = index + 2
            raw_value: list[str] = []
            while end < len(command):
                if command[end] == "\\" and end + 1 < len(command):
                    raw_value.extend((command[end], command[end + 1]))
                    end += 2
                    continue
                if command[end] == "'":
                    break
                raw_value.append(command[end])
                end += 1
            if end >= len(command):
                normalized.append(command[index:])
                break
            decoded = _decode_bash_ansi_c("".join(raw_value))
            normalized.append(_quote_posix_token(decoded))
            index = end + 1
            continue
        if character == "$" and command[index + 1 : index + 2] == '"':
            normalized.append('"')
            quote = '"'
            index += 2
            continue
        normalized.append(character)
        if character in {'"', "'"}:
            quote = character
        index += 1
    return "".join(normalized)


def _posix_shell_tokens(command: str) -> list[str] | None:
    """Split shell text while normalizing POSIX quote concatenation."""
    normalized = _normalize_shell_escapes(command, escape_character="\\")
    return _tokenize_shell(normalized, posix=True)


def _bash_shell_tokens(command: str) -> list[str] | None:
    normalized = _normalize_shell_escapes(command, escape_character="\\")
    return _tokenize_shell(_normalize_bash_quotes(normalized), posix=True)


def _powershell_shell_tokens(command: str) -> list[str] | None:
    normalized = _normalize_shell_escapes(
        command,
        escape_character="`",
        special_escapes=_POWERSHELL_51_SPECIAL_ESCAPES,
    )
    return _tokenize_shell(
        normalized, posix=False, punctuation_chars=f"{_SHELL_CONTROL_CHARACTERS}{{}}"
    )


def _shell_token_views(
    command: str, *, include_escape_normalizations: bool = False
) -> list[list[str]]:
    token_views: list[list[str]] = []
    views = [_shell_tokens(command), _posix_shell_tokens(command)]
    if include_escape_normalizations:
        views.extend((_powershell_shell_tokens(command), _bash_shell_tokens(command)))
    for tokens in views:
        if tokens is not None and tokens not in token_views:
            token_views.append(tokens)
    return token_views


def _strip_shell_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def _is_shell_command_separator(token: str) -> bool:
    return bool(token) and all(
        character in _SHELL_COMMAND_SEPARATOR_CHARACTERS for character in token
    )


def _is_shell_redirection(token: str) -> bool:
    return (
        bool(token)
        and any(character in "<>" for character in token)
        and all(character in "<>&|" for character in token)
    )


def _is_shell_file_descriptor(token: str) -> bool:
    return token.isdecimal() or token == "*"


def _without_shell_redirections(tokens: list[str]) -> list[str]:
    command_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        if (
            _is_shell_file_descriptor(tokens[index])
            and index + 2 < len(tokens)
            and _is_shell_redirection(tokens[index + 1])
            and not _is_shell_command_separator(tokens[index + 2])
        ):
            index += 3
            continue
        if (
            _is_shell_redirection(tokens[index])
            and index + 1 < len(tokens)
            and not _is_shell_command_separator(tokens[index + 1])
        ):
            index += 2
            continue
        command_tokens.append(tokens[index])
        index += 1
    return command_tokens


def _find_closing_backtick(command: str, start: int) -> int | None:
    index = start
    while index < len(command):
        if command[index] == "\\":
            index += 2
            continue
        if command[index] == "`":
            return index
        index += 1
    return None


def _find_closing_shell_parenthesis(command: str, start: int) -> int | None:
    quote: str | None = None
    depth = 1
    index = start
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == "`":
            closing_backtick = _find_closing_backtick(command, index + 1)
            if closing_backtick is None:
                return None
            index = closing_backtick + 1
            continue
        if character == quote:
            quote = None
        elif quote is None and character in {'"', "'"}:
            quote = character
        elif quote is None and character == "(":
            depth += 1
        elif quote is None and character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _shell_substitution_bodies(command: str) -> list[str]:
    bodies: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == "`":
            closing_backtick = _find_closing_backtick(command, index + 1)
            if closing_backtick is not None:
                bodies.append(command[index + 1 : closing_backtick])
                index = closing_backtick + 1
                continue
            index += 2
            continue
        if (
            character == "$"
            and command[index + 1 : index + 2] == "("
            and command[index + 2 : index + 3] != "("
        ):
            closing_parenthesis = _find_closing_shell_parenthesis(command, index + 2)
            if closing_parenthesis is not None:
                bodies.append(command[index + 2 : closing_parenthesis])
                index = closing_parenthesis + 1
                continue
        if character == quote:
            quote = None
        elif quote is None and character in {'"', "'"}:
            quote = character
        index += 1
    return bodies


def _has_shell_control_syntax(
    command: str, *, escape_character: str, backticks_execute: bool
) -> bool:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if character == escape_character:
            escaped = True
            continue
        if (backticks_execute and character == "`") or (
            character == "$" and command[index + 1 : index + 2] == "("
        ):
            return True
        if quote is None and character in _SHELL_CONTROL_CHARACTERS:
            return True
        if character == quote:
            quote = None
        elif quote is None and character in {'"', "'"}:
            quote = character
    return False


def _is_simple_shell_command(command: str) -> bool:
    trailing_backslashes = len(command) - len(command.rstrip("\\"))
    if trailing_backslashes % 2:
        return False
    control_command = command.lstrip(_SHELL_WHITESPACE)
    if control_command.startswith(("& ", "&\t")):
        control_command = control_command[1:]
    if _has_shell_control_syntax(
        control_command, escape_character="\\", backticks_execute=True
    ) or _has_shell_control_syntax(control_command, escape_character="`", backticks_execute=False):
        return False
    token_views = (_shell_tokens(command), _posix_shell_tokens(command))
    return any(tokens is not None and bool(tokens) for tokens in token_views)


def _shell_executable_name(token: str) -> str:
    normalized = _strip_shell_quotes(token).replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].casefold()


def _is_git_executable(token: str) -> bool:
    return _shell_executable_name(token) in _GIT_EXECUTABLES


def _is_quote_concatenated_git_executable(token: str) -> bool:
    return any(quote in token for quote in {'"', "'"}) and _is_git_executable(
        token.replace('"', "").replace("'", "")
    )


def _is_shell_assignment(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", token) is not None


def _sudo_option_end(tokens: list[str], index: int, end: int) -> int | None:
    option = _strip_shell_quotes(tokens[index])
    if option.startswith("--"):
        if option in _SUDO_OPTIONS_WITH_VALUE:
            return index + 2 if index + 1 < end else None
        if any(
            option.startswith(f"{value_option}=")
            for value_option in _SUDO_OPTIONS_WITH_VALUE
            if value_option.startswith("--")
        ):
            return index + 1
        if option in _SUDO_COMMAND_FLAGS or option.startswith("--preserve-env="):
            return index + 1
        return None

    if not option.startswith("-") or len(option) < 2:
        return None
    for offset, short_option in enumerate(option[1:], start=1):
        normalized = f"-{short_option}"
        if normalized in _SUDO_OPTIONS_WITH_VALUE:
            if offset + 1 < len(option):
                return index + 1
            return index + 2 if index + 1 < end else None
        if normalized not in _SUDO_COMMAND_FLAGS:
            return None
    return index + 1


def _sudo_command_index(tokens: list[str], sudo_index: int, end: int) -> int | None:
    cursor = sudo_index + 1
    options_allowed = True
    while cursor < end:
        if _is_shell_assignment(tokens[cursor]):
            options_allowed = False
            cursor += 1
            continue
        if options_allowed and tokens[cursor] == "--":
            options_allowed = False
            cursor += 1
            continue
        if options_allowed:
            option_end = _sudo_option_end(tokens, cursor, end)
            if option_end is not None:
                cursor = option_end
                continue
        return cursor
    return None


def _command_builtin_command_index(tokens: list[str], command_index: int, end: int) -> int | None:
    cursor = command_index + 1
    while cursor < end:
        argument = _strip_shell_quotes(tokens[cursor])
        if argument == "--":
            return cursor + 1 if cursor + 1 < end else None
        if re.fullmatch(r"-[pVv]+", argument):
            if "v" in argument.casefold():
                return None
            cursor += 1
            continue
        if argument.startswith("-"):
            return None
        return cursor
    return None


def _exec_builtin_command_index(tokens: list[str], exec_index: int, end: int) -> int | None:
    cursor = exec_index + 1
    while cursor < end:
        argument = _strip_shell_quotes(tokens[cursor])
        if argument == "--":
            return cursor + 1 if cursor + 1 < end else None
        if not argument.startswith("-") or argument == "-":
            return cursor

        for offset, short_option in enumerate(argument[1:], start=1):
            if short_option == "a":
                if offset + 1 < len(argument):
                    cursor += 1
                else:
                    cursor += 2
                break
            if short_option not in "cl":
                return None
        else:
            cursor += 1
            continue
        if cursor > end:
            return None
    return None


def _nohup_command_index(tokens: list[str], nohup_index: int, end: int) -> int | None:
    cursor = nohup_index + 1
    if cursor >= end:
        return None
    argument = _strip_shell_quotes(tokens[cursor])
    if argument in {"--help", "--version"}:
        return None
    if argument == "--":
        return cursor + 1 if cursor + 1 < end else None
    if argument.startswith("-"):
        return None
    return cursor


def _transparent_prefix_command_index(tokens: list[str], index: int, end: int) -> int | None:
    executable = _shell_executable_name(tokens[index])
    if executable == "sudo":
        return _sudo_command_index(tokens, index, end)
    if executable == "command":
        return _command_builtin_command_index(tokens, index, end)
    if executable == "exec":
        return _exec_builtin_command_index(tokens, index, end)
    if executable == "nohup":
        return _nohup_command_index(tokens, index, end)
    return None


def _is_wrapper_command_position(tokens: list[str], index: int) -> bool:
    segment_start = index
    while segment_start > 0 and not _is_shell_command_separator(tokens[segment_start - 1]):
        segment_start -= 1
    segment_end = index + 1
    while segment_end < len(tokens) and not _is_shell_command_separator(tokens[segment_end]):
        segment_end += 1

    cursor = segment_start
    while cursor < index and _is_shell_assignment(tokens[cursor]):
        cursor += 1

    while cursor < index:
        if _shell_executable_name(tokens[cursor]) not in _TRANSPARENT_SHELL_PREFIXES:
            return False
        command_index = _transparent_prefix_command_index(tokens, cursor, segment_end)
        if command_index is None:
            return False
        cursor = command_index
    return cursor == index


def _is_powershell_payload_option(argument: str) -> bool:
    if argument.startswith("/"):
        argument = f"-{argument[1:]}"
    if len(argument) < 2:
        return False
    return (
        "-command".startswith(argument)
        or "-encodedcommand".startswith(argument)
        or "-commandwithargs".startswith(argument)
        or argument in {"-cwa", "-ec"}
    )


def _has_posix_shell_payload_option(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return False
        if argument in _POSIX_SHELL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if not argument.startswith("-"):
            return False
        if not argument.startswith("--") and re.fullmatch(r"-[a-z]*c[a-z]*", argument):
            return True
        index += 1
    return False


def _has_powershell_payload_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = f"-{argument[1:]}" if argument.startswith("/") else argument
        if len(normalized) >= 2 and "-file".startswith(normalized):
            return False
        if _is_powershell_payload_option(normalized):
            return True
    return False


def _is_opaque_shell_wrapper(command: str) -> bool:
    for raw_tokens in _shell_token_views(command, include_escape_normalizations=True):
        tokens = _without_shell_redirections(raw_tokens)
        if any(
            token == "&" and (next_token in {"(", "{"} or next_token.startswith("$"))
            for token, next_token in zip(tokens, tokens[1:], strict=False)
        ):
            # PowerShell can resolve a destructive alias or cmdlet through an
            # expression/script block; evaluating it here would execute user input.
            return True
        for index, token in enumerate(tokens):
            if not _is_wrapper_command_position(tokens, index):
                continue
            executable = _shell_executable_name(token)
            if executable in _POWERSHELL_EVAL_EXECUTABLES:
                return True
            end = index + 1
            while end < len(tokens) and not _is_shell_command_separator(tokens[end]):
                end += 1
            if executable in _ENV_EXECUTABLES and end > index + 1:
                return True
            arguments = [
                _strip_shell_quotes(argument).casefold() for argument in tokens[index + 1 : end]
            ]
            if executable in _POSIX_SHELL_EXECUTABLES and _has_posix_shell_payload_option(
                arguments
            ):
                return True
            if executable in _POWERSHELL_EXECUTABLES and _has_powershell_payload_option(arguments):
                return True
            if executable in _CMD_EXECUTABLES and any(
                argument.startswith(("/c", "/k")) for argument in arguments
            ):
                return True
    return False


def _parse_git_invocation(tokens: list[str], executable_index: int) -> tuple[str, list[str]] | None:
    end = executable_index + 1
    while end < len(tokens) and not _is_shell_command_separator(tokens[end]):
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
    invocations: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw_tokens in _shell_token_views(command, include_escape_normalizations=True):
        tokens = _without_shell_redirections(raw_tokens)
        for index, token in enumerate(tokens):
            if not _is_git_executable(token):
                continue
            invocation = _parse_git_invocation(tokens, index)
            if invocation is None:
                continue
            key = (invocation[0], tuple(invocation[1]))
            if key not in seen:
                seen.add(key)
                invocations.append(invocation)
    return invocations


def _is_git_long_option(argument: str, option: str) -> bool:
    normalized = argument.casefold()
    option = option.casefold()
    name = normalized.partition("=")[0]
    return (
        name.startswith("--")
        and name != "--"
        and not name.startswith("--no-")
        and option.startswith(name)
    )


def _has_force_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        if any(
            _is_git_long_option(argument, option)
            for option in ("--force", "--force-if-includes", "--force-with-lease")
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
        if _is_git_long_option(argument, "--delete"):
            return True
        if re.fullmatch(r"-[a-z]*d[a-z]*", normalized):
            return True
    return False


def _is_force_refspec(argument: str) -> bool:
    """Return True for Git force refspecs such as ``+main`` or ``+src:dst``."""
    return not argument.startswith("-") and argument.startswith("+")


def _is_delete_refspec(argument: str) -> bool:
    """Return True for Git delete refspecs such as ``:main`` or ``+:refs/heads/main``."""
    if argument.startswith("-"):
        return False
    body = argument[1:] if argument.startswith("+") else argument
    return body.startswith(":") and len(body) > 1


def _has_dangerous_push_refspec(arguments: list[str]) -> bool:
    """Detect force/delete refspecs among push positionals, including after ``--``."""
    seen_double_dash = False
    for argument in arguments:
        if not seen_double_dash:
            if argument == "--":
                seen_double_dash = True
                continue
            if argument.startswith("-"):
                continue
        if _is_force_refspec(argument) or _is_delete_refspec(argument):
            return True
    return False


def _has_dangerous_push_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        if any(_is_git_long_option(argument, option) for option in ("--mirror", "--prune")):
            return True
    return False


def _is_dangerous_git_command(command: str) -> bool:
    for subcommand, arguments in _git_invocations(command):
        if subcommand == "clean" and _has_force_option(arguments):
            return True
        if subcommand == "push" and (
            _has_force_option(arguments)
            or _has_branch_delete_option(arguments)
            or _has_dangerous_push_option(arguments)
            or _has_dangerous_push_refspec(arguments)
        ):
            return True
        if subcommand == "reset" and any(
            _is_git_long_option(argument, "--hard") for argument in arguments
        ):
            return True
        if subcommand == "branch" and _has_branch_delete_option(arguments):
            return True
    return False


def _is_rm_executable(token: str) -> bool:
    executable = _shell_executable_name(token)
    return executable in _RM_EXECUTABLES or executable in _POWERSHELL_REMOVE_ITEM_EXECUTABLES


def _is_powershell_remove_item_executable(token: str) -> bool:
    executable = _shell_executable_name(token)
    if executable in _POWERSHELL_REMOVE_ITEM_EXECUTABLES:
        return True
    return (
        any(quote in token for quote in {'"', "'"})
        and _shell_executable_name(token.replace('"', "").replace("'", ""))
        in _POWERSHELL_REMOVE_ITEM_EXECUTABLES
    )


def _is_quote_concatenated_rm_executable(token: str) -> bool:
    return any(quote in token for quote in {'"', "'"}) and _is_rm_executable(
        token.replace('"', "").replace("'", "")
    )


def _is_chmod_executable(token: str) -> bool:
    return _shell_executable_name(token) in _CHMOD_EXECUTABLES


def _is_quote_concatenated_chmod_executable(token: str) -> bool:
    return any(quote in token for quote in {'"', "'"}) and _is_chmod_executable(
        token.replace('"', "").replace("'", "")
    )


def _is_powershell_switch_enabled(argument: str, option: str) -> bool:
    normalized = argument.casefold()
    name, separator, value = normalized.partition(":")
    if name != option:
        return False
    return not separator or value not in {"$false", "false", "0"}


def _has_recursive_rm_option(arguments: list[str], *, powershell: bool = False) -> bool:
    """Return True when rm arguments enable recursive deletion in any order."""
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        if powershell:
            option = normalized.partition(":")[0]
            if option in _POWERSHELL_RECURSE_OPTIONS and _is_powershell_switch_enabled(
                normalized, option
            ):
                return True
            continue
        if normalized.partition("=")[0] in _RM_RECURSIVE_LONG_OPTION_PREFIXES:
            return True
        if re.fullmatch(r"-[a-z]*r[a-z]*", normalized):
            return True
    return False


def _has_powershell_whatif_option(arguments: list[str]) -> bool:
    for argument in arguments:
        normalized = argument.casefold()
        if normalized == "--":
            break
        option, separator, value = normalized.partition(":")
        if option == "-whatif" and (not separator or value in {"$true", "true", "1"}):
            return True
    return False


def _is_world_writable_chmod_mode(argument: str) -> bool:
    """Return True for numeric modes that grant world rwx (e.g. 777, 0777)."""
    return re.fullmatch(r"0*777", argument) is not None


def _command_segment_arguments(tokens: list[str], executable_index: int) -> list[str]:
    end = executable_index + 1
    while end < len(tokens) and not _is_shell_command_separator(tokens[end]):
        end += 1
    return [_strip_shell_quotes(argument) for argument in tokens[executable_index + 1 : end]]


def _is_dangerous_rm_command(command: str) -> bool:
    """Detect recursive rm regardless of option order, long options, or quoting."""
    for raw_tokens in _shell_token_views(command, include_escape_normalizations=True):
        tokens = _without_shell_redirections(raw_tokens)
        for index, token in enumerate(tokens):
            if not _is_wrapper_command_position(tokens, index):
                continue
            if not (_is_rm_executable(token) or _is_quote_concatenated_rm_executable(token)):
                continue
            arguments = _command_segment_arguments(tokens, index)
            powershell = _is_powershell_remove_item_executable(token)
            if _has_recursive_rm_option(arguments, powershell=powershell) and not (
                powershell and _has_powershell_whatif_option(arguments)
            ):
                return True
    return False


def _is_dangerous_chmod_command(command: str) -> bool:
    """Detect chmod modes that grant world rwx, including with -R / leading zeros."""
    for raw_tokens in _shell_token_views(command, include_escape_normalizations=True):
        tokens = _without_shell_redirections(raw_tokens)
        for index, token in enumerate(tokens):
            if not _is_wrapper_command_position(tokens, index):
                continue
            if not (_is_chmod_executable(token) or _is_quote_concatenated_chmod_executable(token)):
                continue
            if any(
                _is_world_writable_chmod_mode(argument)
                for argument in _command_segment_arguments(tokens, index)
            ):
                return True
    return False


def _has_dangerous_shell_substitution(command: str, patterns: list[re.Pattern[str]]) -> bool:
    for body in _shell_substitution_bodies(command):
        if (
            _is_opaque_shell_wrapper(body)
            or _is_dangerous_git_command(body)
            or _is_dangerous_rm_command(body)
            or _is_dangerous_chmod_command(body)
            or any(pattern.search(body) for pattern in patterns)
            or _has_dangerous_shell_substitution(body, patterns)
        ):
            return True
    return False


def _has_output_option(arguments: list[str]) -> bool:
    return any(_is_git_long_option(argument, "--output") for argument in arguments)


def _has_mutating_branch_option(argument: str) -> bool:
    normalized = argument.casefold()
    if any(_is_git_long_option(argument, option) for option in _BRANCH_MUTATING_OPTIONS):
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
    if not _is_simple_shell_command(command):
        return False

    raw_tokens = _shell_tokens(command)
    if not raw_tokens:
        return False
    raw_executable_index = 1 if raw_tokens[0] == "&" else 0
    if raw_executable_index >= len(raw_tokens):
        return False
    raw_executable = raw_tokens[raw_executable_index]
    if not _is_git_executable(raw_executable) and not _is_quote_concatenated_git_executable(
        raw_executable
    ):
        return False

    found_read_only_invocation = False
    found_mutating_invocation = False
    for raw_tokens in _shell_token_views(command):
        tokens = _without_shell_redirections(raw_tokens)
        executable_index = 0
        if tokens[0] == "&":
            executable_index = 1
        if executable_index >= len(tokens) or not _is_git_executable(tokens[executable_index]):
            continue

        invocation = _parse_git_invocation(tokens, executable_index)
        if invocation is None:
            continue
        subcommand, arguments = invocation
        if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
            if _has_output_option(arguments):
                found_mutating_invocation = True
            else:
                found_read_only_invocation = True
            continue
        if subcommand == "branch":
            if _is_read_only_branch(arguments):
                found_read_only_invocation = True
            else:
                found_mutating_invocation = True
            continue
        if not any(character in subcommand for character in {'"', "'"}):
            found_mutating_invocation = True
    return found_read_only_invocation and not found_mutating_invocation


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
        re.compile(
            rf"^(ls|cat|head|tail|wc|echo|pwd|date|which|type)"
            rf"(?=[{_SHELL_WHITESPACE}]|$)"
        ),
        re.compile(
            rf"^(pytest|python[{_SHELL_WHITESPACE}]+-m"
            rf"[{_SHELL_WHITESPACE}]+pytest|ruff|mypy)"
            rf"(?=[{_SHELL_WHITESPACE}]|$)"
        ),
        re.compile(
            rf"^npm[{_SHELL_WHITESPACE}]+"
            rf"(test|run[{_SHELL_WHITESPACE}]+lint|run[{_SHELL_WHITESPACE}]+test)"
            rf"(?=[{_SHELL_WHITESPACE}]|$)"
        ),
    ]
    # PowerShell command and executable resolution is case-insensitive on the
    # Windows execution path. Compile the complete set consistently so casing
    # the invoked command cannot turn destructive input into an AUTO bypass.
    DANGEROUS_PATTERNS = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"(^|[\s;&|])rm\s+-[a-z]*r[a-z]*\b",
            r"\bremove-item\b"
            r"(?=[^;\r\n|]*[ \t]-recurse(?::\$?true)?(?:[ \t]|$))"
            r"(?![^;\r\n|]*[ \t]-whatif(?::\$?true)?(?:[ \t]|$))",
            r"\bgit\s+push\b[^\r\n;&|<>]*(?<!\S)(?:--force(?:-with-lease|-if-includes)?|-[a-z]*f[a-z]*)\b",
            r"\bgit\s+clean\b[^\r\n;&|<>]*(?<!\S)(?:--force\b|-[a-z]*f[a-z]*\b)",
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
            if (
                _is_opaque_shell_wrapper(cmd)
                or _has_dangerous_shell_substitution(cmd, self.DANGEROUS_PATTERNS)
                or _is_dangerous_git_command(cmd)
                or _is_dangerous_rm_command(cmd)
                or _is_dangerous_chmod_command(cmd)
                or any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS)
            ):
                return True
            if self._match_rules(self.allow_rules, normalized_tool, cmd):
                return False
            if _is_read_only_git_command(cmd) or (
                _is_simple_shell_command(cmd)
                and any(pattern.search(cmd) for pattern in self.AUTO_SAFE_PATTERNS)
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
            return (
                _is_opaque_shell_wrapper(cmd)
                or _has_dangerous_shell_substitution(cmd, self.DANGEROUS_PATTERNS)
                or _is_dangerous_git_command(cmd)
                or _is_dangerous_rm_command(cmd)
                or _is_dangerous_chmod_command(cmd)
                or any(pattern.search(cmd) for pattern in self.DANGEROUS_PATTERNS)
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
        normalized_subject = (
            cmd.strip(_SHELL_WHITESPACE) if normalized_tool in _SHELL_COMMAND_TOOLS else cmd.strip()
        )
        for rule in rules:
            parsed = _parse_prefix_rule(rule)
            if parsed is None:
                continue
            rule_tool, prefix = parsed
            if rule_tool != normalized_tool:
                continue
            if normalized_subject.startswith(prefix):
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
        command = str(tool_input.get("command", "")).strip(_SHELL_WHITESPACE)
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
