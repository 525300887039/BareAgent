# Design

## Boundary

Keep permission classification in `bareagent.permission.guard`; the shell
handler continues to execute only after the guard decision. Do not invoke Git
or inspect repository configuration during classification.

## Token recognition

Use `shlex` only to split the command into quote-normalized tokens and shell
separators. Recognize an executable by its final path component (`git` or
`git.exe`) so Unix paths, Windows paths, quoted paths, and PowerShell's `&`
form share one path. Parse past known value-taking Git global options and
option flags until the first subcommand token.

The helper returns normalized `(subcommand, arguments)` records. Dangerous and
read-only classification both consume these records, avoiding two divergent
regular-expression grammars. Existing Git regular expressions remain a
conservative fallback if tokenization fails; unrelated dangerous expressions
are unchanged.

## Classification

- `push`: dangerous when an argument is a force long option or a short option
  bundle containing `f`.
- `clean`: dangerous under the same force-option rule.
- `reset`: dangerous with `--hard`.
- `branch`: dangerous with delete flags `-d`, `-D`, or `--delete`.
- `status`, `log`, `diff`, `show`, and non-mutating `branch` forms remain the
  DEFAULT-mode read-only set. A branch positional argument or mutating option
  is not auto-safe.

Dangerous classification remains before allow rules. AUTO therefore requests
confirmation, and a fail-closed caller denies the request through the existing
`ask_user` contract.

## Compatibility and failure behavior

Tokenization is local and side-effect free. If quoting is malformed, retain
the existing regex fallback rather than treating the input as newly safe. MCP
tools never enter shell classification. BYPASS and PLAN ordering is unchanged.
