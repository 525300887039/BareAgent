# Git invocation permission audit

## Evidence on `739de88`

`PermissionGuard` used regular expressions that required a literal, unquoted
`git` immediately followed by the subcommand. The following valid commands all
returned `requires_confirm=False` and `is_dangerous=False` in AUTO mode:

- `git.exe push --force`
- `git.exe clean -fdx`
- `git.exe reset --hard`
- `git --no-pager push --force`
- `git -C . clean -fdx`
- `git -c core.quotePath=false reset --hard`
- `git "push" "--force"`
- `git push "-f"`
- `& "C:\Program Files\Git\cmd\git.exe" clean -fdx`

Safe `push -h` probes against Git 2.53 confirmed that the executable, global
option, and quoting forms are accepted without running a destructive action.
The command handler passes the original string to PowerShell on Windows or
`bash -lc` elsewhere, so a false negative reaches the real shell unchanged.

The same literal assumption caused DEFAULT mode to prompt unnecessarily for
safe forms such as `git.exe status`, `git -C . status`, and quoted/full-path
`status` invocations. Existing dangerous-pattern tests passed because they
only covered plain `git <subcommand>` spelling.

Main-session review also found that the broad `git branch` safe expression
classified `git branch -d/-D` as read-only. Branch deletion is therefore part
of the destructive regression matrix.
