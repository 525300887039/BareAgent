# Code Review

Use this skill when you need a practical review checklist for correctness, safety, performance, and maintainability.

## Review Order

- Start with behavior and risk.
- Then inspect security-sensitive edges.
- Then inspect performance hotspots.
- Finish with readability and tests.

## Correctness

- Does the change satisfy the stated task?
- Are error cases handled deliberately?
- Are assumptions validated or documented?
- Are return values consistent across branches?
- Are default values safe?
- Is state updated in the right order?
- Can stale data survive after an update?
- Are identifiers stable and deterministic when tests need them?

## Security

- Check command execution inputs for injection risk.
- Check file path handling for traversal or escape risk.
- Check network calls for accidental secret leakage.
- Check tool outputs for overexposure of sensitive data.
- Check whether permissions are bypassed unintentionally.
- Check whether untrusted input reaches shell commands directly.

## Performance

- Look for repeated full-file scans in hot paths.
- Check whether expensive work can be cached safely.
- Avoid unnecessary large prompt injections.
- Prefer on-demand loading for heavy reference content.
- Keep loops linear unless a stronger structure is required.
- Watch for duplicate subprocess calls.

## Readability

- Prefer explicit names over clever abstractions.
- Keep handlers thin and delegate logic to modules.
- Make formatting output easy for the agent to parse.
- Avoid hidden side effects in helper functions.
- Keep validation errors specific.

## Testing

- Add tests for the happy path.
- Add tests for invalid input where behavior matters.
- Add tests for regressions around integration seams.
- Prefer deterministic outputs that are easy to assert.
- Verify new tools are reachable from the registry path.

## Review Findings Format

- Report the most severe issue first.
- Include file and line when possible.
- Explain the concrete failure mode.
- Suggest the smallest correction that closes the gap.

## When No Findings Exist

- Say so explicitly.
- Note any remaining test gaps or assumptions.
- Separate confirmed behavior from inferred behavior.
