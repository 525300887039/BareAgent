# Testing

Use this skill when writing or reviewing tests for agent loops, tool handlers, prompt assembly, or filesystem behavior.

## Core Principles

- Test behavior, not implementation trivia.
- Keep tests deterministic.
- Prefer local fixtures over shared global state.
- Make failure messages easy to understand.

## AAA Structure

- Arrange the minimal state needed.
- Act with one clear trigger.
- Assert the smallest useful surface.
- Split cases instead of hiding multiple expectations in one test.

## What To Cover

- Happy path behavior.
- Validation failures that users or the agent can trigger.
- Ordering-sensitive outputs when they affect prompt quality.
- Integration seams where objects are wired together.
- Empty-state behavior.
- State transitions across multiple calls.

## Boundary Conditions

- Zero items.
- One item.
- Multiple items.
- Missing required fields.
- Unknown IDs or names.
- Invalid enum-like values.
- Whitespace-only inputs when relevant.

## Mock Strategy

- Mock provider calls at the boundary, not deep inside data objects.
- Use simple fake classes when behavior is small and stateful.
- Prefer monkeypatch for subprocess or environment access.
- Avoid mocking code you can exercise cheaply for real.

## Assertions

- Assert exact text when prompt wording matters.
- Assert substrings when the surrounding text is intentionally flexible.
- Assert ordering when the agent depends on ordered lists.
- Do not assert incidental whitespace unless required.

## Filesystem Tests

- Use `tmp_path` for skill directories and generated files.
- Keep paths relative to the temp workspace.
- Write UTF-8 text explicitly.

## Review Checklist

- Can this test fail for the right reason?
- Does it break if the intended behavior regresses?
- Is there a simpler fixture setup?
- Is the name specific about the scenario?

## Anti-Patterns

- One test covering multiple unrelated behaviors.
- Mocking the method under test.
- Asserting internal counters unless they are part of behavior.
- Requiring network or real API keys for unit tests.
