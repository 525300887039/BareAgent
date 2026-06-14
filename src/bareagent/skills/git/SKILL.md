# Git Workflow

Use this skill when you need to prepare commits, choose a branch name, or check whether a change is ready to land.

## Goals

- Keep history readable.
- Make branch purpose obvious.
- Prefer small, reviewable commits.
- Avoid mixing unrelated changes.

## Branch Naming

- Use lowercase words and hyphens only.
- Prefer `feature/<area>-<summary>` for new work.
- Prefer `fix/<area>-<summary>` for bug fixes.
- Prefer `chore/<area>-<summary>` for maintenance.
- Prefer `docs/<area>-<summary>` for documentation-only work.
- Keep names short enough to read in `git branch`.
- Encode the user-facing intent, not the implementation detail.
- Good: `feature/planning-skills-loader`
- Good: `fix/repl-nag-reminder`
- Bad: `misc/stuff`
- Bad: `feature/final-version-v2`

## Commit Structure

- Follow Conventional Commits.
- Format: `<type>(<scope>): <summary>`
- Omit the scope if it adds no value.
- Keep the summary in imperative mood.
- Keep the summary under 72 characters when practical.
- Types:
- `feat`: user-visible capability
- `fix`: behavior correction
- `refactor`: internal code change without behavior change
- `test`: test-only change
- `docs`: documentation-only change
- `chore`: maintenance or tooling

## Commit Message Examples

- `feat(planning): add in-memory todo manager`
- `fix(main): inject nag reminder before latest user turn`
- `test(skills): cover skill scanning and loading`
- `docs(readme): explain planning layer`

## Commit Hygiene

- Review `git diff --stat` before committing.
- Stage only files related to one logical change.
- Separate refactors from behavior changes when possible.
- Do not include generated files unless required by the repo.
- Mention follow-up work in the body, not the summary.

## Before Commit

- Run the narrowest relevant test command first.
- Re-read changed error messages and user-facing text.
- Check for accidental debug prints.
- Check for unrelated formatting churn.
- Confirm new files are included.

## When Unsure

- Prefer two small commits over one mixed commit.
- Prefer a boring branch name over a clever one.
- Ask whether the commit title would still make sense in six months.
