# Permission bypass evidence

## Reproduced classification failures

The final security review of the preceding Git-classification task found these
side-effect-free classification results on local `main`:

- `git status; Set-Content pwned.txt x` returned
  `DEFAULT requires_confirm=False` because the read-only Git parser ignored the
  trailing command.
- `ls; touch pwned` returned `DEFAULT requires_confirm=False` because an
  `AUTO_SAFE_PATTERNS` regular expression matched only the prefix.
- `powershell -Command "git.exe clean -fdx"` and
  `cmd /c "git.exe reset --hard"` returned
  `AUTO requires_confirm=False` and `is_dangerous=False`.
- `g""it push --force`, `git p'u'sh --force`, and
  `git push --fo'r'ce` returned non-dangerous results despite being valid Unix
  shell quote concatenation.
- Follow-up tokenization probes confirmed that `git status` followed by an
  unquoted newline and another command, `git status > output.txt`,
  `ls 2>>output.txt`, and `echo "$(Set-Content x y)"` also received the
  DEFAULT-mode safe shortcut. These forms can execute or write beyond the
  nominally safe prefix.
- POSIX backslash-newline continuation can spell `git push --force` across
  physical lines while remaining non-dangerous in AUTO, and unquoted grouping
  expressions such as `echo (Set-Content ...)` / `echo @(Set-Content ...)`
  remain executable despite the safe `echo` prefix.

No destructive command was executed. The review also verified that recognized
destructive Git forms run before allow rules, PLAN/BYPASS ordering is intact,
and `git status; git push --force` is already caught by the second invocation.

## Scope decision

Compound commands must lose only the DEFAULT-mode safe shortcut; this avoids
changing AUTO's documented allow-by-default behavior for unknown commands.
Opaque Windows wrapper launchers should follow the existing blanket treatment
of `sh -c`, because recursively parsing multiple shell grammars would be both
larger and less reliable. Unix quote concatenation should be normalized through
a POSIX token view while preserving the current Windows token view.
