# Gate native PDF blocks by provider

## Goal

Prevent Anthropic-native PDF document blocks from being enabled for provider
transports that cannot serialize them, even when an OpenAI-compatible endpoint
uses a Claude-like model name.

## Requirements

- Native PDF input requires both a provider transport that supports Anthropic
  document blocks and a model that supports native PDF input.
- The `pdf_in` override may override model detection, but must not bypass the
  provider transport boundary.
- Anthropic + Claude behavior, explicit PDF disablement, image capability
  detection, and the existing pypdf fallback must remain unchanged.
- Add a regression test for an OpenAI-compatible provider using a Claude model
  name, covering the provider gate rather than only the model-prefix table.

## Acceptance Criteria

- [x] OpenAI-compatible providers never enable native PDF blocks solely because
      their model name starts with a Claude prefix.
- [x] `pdf_in = true` cannot force an unsupported provider transport to receive
      Anthropic document blocks.
- [x] Anthropic Claude models still enable native PDF blocks automatically, and
      `pdf_in = false` still disables them.
- [x] Targeted regression tests and the repository quality gate pass.

## Notes

- Confirmed reproduction on `main` before implementation: `_build_handlers`
  returns `pdf_input_enabled=True` for `OpenAIProvider(model="claude-3-5-sonnet-20241022")`,
  after which the OpenAI adapter serializes the native `document` block as
  ordinary JSON/base64 tool text.
- This is a lightweight bug fix and remains PRD-only.
