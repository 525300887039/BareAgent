# Design

## Boundary

Keep all classification inside `bareagent.permission.guard`. The guard parses
shell text without executing it; command handlers, Git configuration, and the
permission-mode ordering remain unchanged.

## Whole-command safety

DEFAULT-mode auto-safe classification is valid only when the complete shell
input is a single simple command. Detect unquoted command separators, CR/LF,
redirection operators, grouping parentheses, and command substitution before
accepting either the read-only Git path or `AUTO_SAFE_PATTERNS`. Separator,
redirection, and parenthesis characters inside literal quotes remain ordinary
argument text, but `$()` / backtick substitution inside double quotes is still
executable shell syntax. Preserve only the existing leading PowerShell `&`
call-operator shape used for a quoted Git executable. If tokenization is
malformed or the command contains control syntax, do not grant the DEFAULT safe
shortcut.

This check is deliberately scoped to automatic safety. AUTO retains its
existing allow-by-default semantics for otherwise unknown commands, while
known opaque shell launchers still force confirmation through dangerous
classification.

## Opaque shell launchers

Extend the existing wrapper boundary (`sh`-family `-c`) to Windows launchers
whose payload cannot be trusted as a top-level command: `powershell` / `pwsh`
with `-Command`, and `cmd` with `/c`. Recognize executable suffixes and quoted
or absolute paths case-insensitively. Treat the launcher itself as dangerous,
so AUTO and `is_dangerous` fail closed without attempting to recursively
interpret a second shell grammar.

## Git quote normalization

Retain the current Windows-preserving tokenization and add normalized POSIX and
PowerShell views for quote concatenation, escape characters, and line
continuations. Feed all token views through the same Git invocation parser and
de-duplicate equivalent records. Strings such as
`g""it p'u'sh --fo'r'ce` and a backslash-continued equivalent must normalize to
the same invocation as `git push --force`, while quoted Windows executable
paths continue to work.

Tokenization failure must never make a command newly safe. Existing literal
dangerous regular expressions remain conservative fallbacks.

## Compatibility and rollback

Do not change safe tool lists, allow/deny prefix semantics, or the order of
BYPASS, PLAN, MCP, and shell checks. Roll back the token views, wrapper helper,
and whole-command safe guard together if focused regressions expose an
incompatible shell form.
