# Changelog

All notable user-visible changes to BareAgent are documented in this file.

## [0.2.0] - 2026-07-22

### Added

- Added provider-independent prompt caching controls, cache economics, Gemini support, and
  GPT-5 cache-read accounting with stable cache breakpoints.
- Added semantic code search and a multi-language repository map backed by tree-sitter,
  reference graphs, PageRank, and token-budgeted rendering.
- Added configurable grep output modes for matching content, file names, or counts.
- Added session forking and tree visualization, including fail-open rendering when a damaged
  lineage sidecar contains pure cycles or self-cycles.
- Added terminal image attachments, vision capability gating, and multimodal `web_fetch`
  results for images and PDF document blocks.

### Changed

- Strengthened PR and main CI with Windows coverage, a dedicated socket suite, pinned Ruff
  formatting, and Pyright standard-mode type checking.
- Made the release workflow reuse the complete CI gate, validate strict release tags and
  hatch-vcs artifact versions, build from a clean `dist/`, and install-test both the wheel
  and sdist before Trusted Publishing can run.
- Pinned third-party GitHub Actions to immutable commit SHAs.

### Fixed

- Hardened permission, tracing privacy, team memory, persistent state, provider configuration,
  streaming tool calls, retries, and code-search/repository-map scoping.
- Fixed session tree rendering so corrupt cyclic lineage cannot hide transcripts or render a
  session more than once.
- Corrected package resource and configuration documentation for installed distributions.

## [0.1.0] - 2026-06-14

- Initial PyPI release of the BareAgent terminal coding agent.

[0.2.0]: https://github.com/525300887039/BareAgent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/525300887039/BareAgent/tree/v0.1.0
