"""Interactive ``bareagent init`` setup wizard + stdlib-only TOML writer.

This module owns "choosing and configuring a provider", so per
``directory-structure.md`` it lives next to ``presets.py`` / ``factory.py``
rather than in a new top-level package. The wizard is a numbered-menu + line
input flow (no full-screen prompt-toolkit dialog -- simpler, cross-platform,
testable). IO is injected via ``input_fn`` / ``output_fn`` so tests can script
answers without touching real stdin.

Writing is stdlib-only: read the existing ``config.local.toml`` text, replace
or insert just the ``[provider]`` section, validate the result with
``tomllib``, then atomically write it back -- no ``tomlkit`` / ``tomli``
dependency (see ``quality-guidelines.md``).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from pathlib import Path

from src.core.fileutil import atomic_write_text
from src.provider.presets import PROVIDER_PRESETS, ProviderPreset

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

# Ordered list of channels shown in the menu. The first five are presets; the
# sixth is the custom OpenAI-compatible branch.
_MENU_PRESET_IDS = ("deepseek", "openai", "anthropic", "qwen", "glm")
_CUSTOM_CHOICE_LABEL = "Third-party OpenAI-compatible (custom base_url)"

# Field order used when rendering the [provider] table so the written file is
# stable and readable.
_PROVIDER_FIELD_ORDER = (
    "name",
    "model",
    "base_url",
    "api_key",
    "api_key_env",
    "wire_api",
)


def run_setup_wizard(
    *,
    config_path: Path,
    input_fn: InputFn | None = None,
    output_fn: OutputFn | None = None,
) -> bool:
    """Run the interactive provider setup wizard.

    Collects a channel choice, model, base_url and API key, then writes the
    ``[provider]`` section into the ``.local`` sibling of *config_path*,
    preserving every other section. Returns ``True`` when the config was
    written, ``False`` when the user cancelled or gave unrecoverable input.

    IO is injectable: *input_fn* defaults to :func:`input` and *output_fn* to
    :func:`print`, so tests drive it with scripted answers and captured output.
    """
    ask = input_fn if input_fn is not None else input
    say = output_fn if output_fn is not None else print

    try:
        choice = _select_channel(ask, say)
    except _WizardCancelled:
        say("Setup cancelled.")
        return False
    if choice is None:
        return False

    if choice == "custom":
        table = _collect_custom(ask, say)
    else:
        table = _collect_preset(choice, ask, say)
    if table is None:
        return False

    local_path = _local_config_path(config_path)
    try:
        _write_provider_section(local_path, table)
    except OSError as exc:
        say(f"Failed to write {local_path}: {exc}")
        return False
    except tomllib.TOMLDecodeError as exc:
        # The in-memory validation in _write_provider_section rejected the
        # spliced result (e.g. the existing file has an oddly-placed `[provider]`
        # line inside a multi-line string). Abort cleanly -- the original file is
        # never touched because validation runs before the atomic write.
        say(f"Refusing to write {local_path}: result would not be valid TOML ({exc}).")
        return False

    say("")
    say(f"Provider configuration written to {local_path}.")
    say("Run `bareagent` to start a session with the configured channel.")
    if "api_key_env" in table:
        say(
            "Remember to export the environment variable "
            f"{table['api_key_env']} before running BareAgent."
        )
    return True


class _WizardCancelled(Exception):
    """Internal signal that the user aborted the wizard (EOF / Ctrl+C)."""


def _ask(ask: InputFn, prompt: str) -> str:
    try:
        return ask(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _WizardCancelled from exc


def _select_channel(ask: InputFn, say: OutputFn) -> str | None:
    """Prompt for a channel; return a preset id, ``"custom"``, or ``None``."""
    say("BareAgent provider setup")
    say("Select a provider channel:")
    for index, preset_id in enumerate(_MENU_PRESET_IDS, start=1):
        preset = PROVIDER_PRESETS[preset_id]
        say(f"  {index}) {preset.display_name}")
    custom_index = len(_MENU_PRESET_IDS) + 1
    say(f"  {custom_index}) {_CUSTOM_CHOICE_LABEL}")

    raw = _ask(ask, f"Channel [1-{custom_index}]: ")
    if not raw:
        say("No channel selected.")
        return None
    try:
        selected = int(raw)
    except ValueError:
        say(f"Invalid choice: {raw!r}. Expected a number 1-{custom_index}.")
        return None
    if selected == custom_index:
        return "custom"
    if 1 <= selected <= len(_MENU_PRESET_IDS):
        return _MENU_PRESET_IDS[selected - 1]
    say(f"Choice out of range: {selected}. Expected 1-{custom_index}.")
    return None


def _collect_preset(
    preset_id: str,
    ask: InputFn,
    say: OutputFn,
) -> dict[str, str] | None:
    preset = PROVIDER_PRESETS[preset_id]
    say("")
    say(f"Configuring {preset.display_name}.")

    model = _prompt_model(preset, ask, say)
    if model is None:
        return None

    base_url = _prompt_base_url(preset.default_base_url, ask, say, required=False)

    table: dict[str, str] = {"name": preset.id, "model": model}
    if base_url:
        table["base_url"] = base_url

    if not _apply_key(table, preset.default_api_key_env, ask, say):
        return None
    return table


def _collect_custom(ask: InputFn, say: OutputFn) -> dict[str, str] | None:
    say("")
    say("Configuring a third-party OpenAI-compatible channel.")

    name = _ask(ask, "Provider route name [openai]: ") or "openai"
    base_url = _prompt_base_url(None, ask, say, required=True)
    if base_url is None:
        return None
    model = _ask(ask, "Model: ")
    if not model:
        say("Model is required.")
        return None

    table: dict[str, str] = {"name": name, "model": model, "base_url": base_url}
    if not _apply_key(table, "OPENAI_API_KEY", ask, say):
        return None
    return table


def _prompt_model(
    preset: ProviderPreset,
    ask: InputFn,
    say: OutputFn,
) -> str | None:
    candidates = preset.candidate_models
    if not candidates:
        model = _ask(ask, "Model: ")
        if not model:
            say("Model is required.")
            return None
        return model

    say("Candidate models:")
    for index, name in enumerate(candidates, start=1):
        say(f"  {index}) {name}")
    raw = _ask(
        ask,
        f"Model [number 1-{len(candidates)}, or type a custom name, default {candidates[0]}]: ",
    )
    if not raw:
        return candidates[0]
    try:
        selected = int(raw)
    except ValueError:
        return raw
    if 1 <= selected <= len(candidates):
        return candidates[selected - 1]
    say(f"Choice out of range: {selected}. Using {candidates[0]}.")
    return candidates[0]


def _prompt_base_url(
    default: str | None,
    ask: InputFn,
    say: OutputFn,
    *,
    required: bool,
) -> str | None:
    if default:
        value = _ask(ask, f"Base URL [{default}]: ")
        return value or default
    value = _ask(ask, "Base URL: ")
    if value:
        return value
    if required:
        say("Base URL is required for a custom OpenAI-compatible channel.")
        return None
    return ""


def _apply_key(
    table: dict[str, str],
    default_api_key_env: str,
    ask: InputFn,
    say: OutputFn,
) -> bool:
    """Collect the key into *table* as ``api_key`` or ``api_key_env``.

    Returns ``True`` on success, ``False`` when required input was missing.
    """
    say("API key storage:")
    say("  1) Write the key in plaintext to config.local.toml (default)")
    say("  2) Use an environment variable instead")
    storage = _ask(ask, "Choice [1-2, default 1]: ")
    if storage == "2":
        env_name = _ask(ask, f"Environment variable name [{default_api_key_env}]: ")
        table["api_key_env"] = env_name or default_api_key_env
        return True

    api_key = _ask(ask, "API key: ")
    if not api_key:
        say("API key is required.")
        return False
    table["api_key"] = api_key
    return True


def _local_config_path(config_path: Path) -> Path:
    """Return the ``.local`` sibling of *config_path*.

    Mirrors :func:`src.main._read_config_file` so the wizard writes exactly the
    file that ``load_config`` later merges as the local override layer.
    """
    return config_path.with_suffix("").with_name(
        config_path.stem + ".local" + config_path.suffix,
    )


def _render_provider_section(provider_table: dict[str, str]) -> str:
    """Render the ``[provider]`` table as a TOML text block (no trailing NL)."""
    lines = ["[provider]"]
    rendered_keys: set[str] = set()
    for key in _PROVIDER_FIELD_ORDER:
        value = provider_table.get(key)
        if not value:
            continue
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        rendered_keys.add(key)
    # Any extra simple keys (defensive: should not normally happen) keep a
    # deterministic order so the file stays stable.
    for key in sorted(provider_table):
        if key in rendered_keys:
            continue
        value = provider_table[key]
        if not value:
            continue
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


def _replace_provider_block(original: str, new_block: str) -> str:
    """Replace or append the top-level ``[provider]`` section in *original*.

    Only the exact top-level table header ``[provider]`` is matched -- not
    ``[provider.xxx]`` sub-tables and not ``[[provider]]`` arrays. The section
    spans from its header line up to (but excluding) the next top-level
    ``[``-prefixed header or EOF.
    """
    lines = original.splitlines()
    start = _find_provider_header(lines)
    new_lines = new_block.splitlines()

    if start is None:
        return _append_provider_block(original, new_block)

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("["):
            end = index
            break

    rebuilt = lines[:start] + new_lines + lines[end:]
    text = "\n".join(rebuilt)
    if original.endswith("\n") or not original:
        text += "\n"
    return text


def _find_provider_header(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == "[provider]":
            return index
    return None


def _append_provider_block(original: str, new_block: str) -> str:
    if not original.strip():
        return new_block + "\n"
    separator = "" if original.endswith("\n") else "\n"
    # Blank line before the appended section keeps the file readable.
    return f"{original}{separator}\n{new_block}\n"


def _write_provider_section(config_path: Path, provider_table: dict[str, str]) -> None:
    """Replace/insert the ``[provider]`` section in *config_path*, atomically.

    Preserves every other section verbatim. The resulting text is validated
    with ``tomllib`` *before* it is written, so a malformed result raises
    instead of corrupting the file.
    """
    original = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    new_block = _render_provider_section(provider_table)
    updated = _replace_provider_block(original, new_block)

    # Validate in memory before touching disk -- a parse failure must abort
    # rather than write a broken file.
    tomllib.loads(updated)

    atomic_write_text(config_path, updated)
