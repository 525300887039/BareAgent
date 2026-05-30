"""Tests for the `bareagent init` setup wizard + stdlib TOML writer (PR2/PR3)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import src.main as main_module
import src.provider.setup as wizard_module
from src.main import ProviderConfig, _has_usable_key, load_config, parse_args, resolve_config_path
from src.provider.setup import _local_config_path, run_setup_wizard


def _scripted_input(answers: list[str]):
    """Return an ``input_fn`` that yields *answers* in order, then raises EOF."""
    it = iter(answers)

    def _fn(_prompt: str) -> str:
        try:
            return next(it)
        except StopIteration as exc:  # pragma: no cover - signals a script too short
            raise EOFError from exc

    return _fn


def _make_base_config(tmp_path: Path) -> Path:
    """Write a minimal valid base config.toml under *tmp_path*; return its path."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'name = "anthropic"',
                'model = "claude-sonnet-4-20250514"',
                'api_key_env = "ANTHROPIC_API_KEY"',
                "",
                "[permission]",
                'mode = "default"',
                "",
                "[ui]",
                "stream = true",
                'theme = "dark"',
                "",
                "[thinking]",
                'mode = "adaptive"',
                "budget_tokens = 10000",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


# --- Round-trip: wizard writes config.local.toml -> load_config parses it ---


def test_wizard_qwen_default_base_url_plaintext_key_roundtrips(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    output: list[str] = []
    # Channel 4 = Qwen; model 1 = qwen-plus; base_url <enter> = default;
    # key storage 1 = plaintext; then the key value.
    answers = ["4", "1", "", "1", "sk-or-not-qwen-key"]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=output.append,
    )

    assert written is True
    local_path = _local_config_path(config_path)
    assert local_path.is_file()

    config = load_config(config_path)
    assert config.provider.name == "qwen"
    assert config.provider.model == "qwen-plus"
    assert config.provider.base_url == ("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert config.provider.api_key == "sk-or-not-qwen-key"


def test_wizard_custom_model_name_is_accepted(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    # Channel 1 = DeepSeek; model = typed custom name; base_url default;
    # plaintext key.
    answers = ["1", "deepseek-custom", "", "1", "plain-key"]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=lambda _msg: None,
    )

    assert written is True
    config = load_config(config_path)
    assert config.provider.name == "deepseek"
    assert config.provider.model == "deepseek-custom"
    assert config.provider.base_url == "https://api.deepseek.com"


# --- Preservation: writing [provider] keeps every other section ---


def test_wizard_preserves_other_sections(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    local_path = _local_config_path(config_path)
    local_path.write_text(
        "\n".join(
            [
                "[memory]",
                "recall_k = 10",
                "",
                "[provider]",
                'name = "anthropic"',
                'model = "claude-sonnet-4-20250514"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    answers = ["1", "1", "", "1", "deepseek-secret"]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=lambda _msg: None,
    )

    assert written is True
    config = load_config(config_path)
    # [provider] was rewritten...
    assert config.provider.name == "deepseek"
    assert config.provider.api_key == "deepseek-secret"
    # ...and [memory] recall_k survived untouched.
    assert config.memory.recall_k == 10


def test_wizard_creates_local_file_when_absent(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    local_path = _local_config_path(config_path)
    assert not local_path.exists()
    # Channel 3 = Anthropic (no default base_url); model 1; base_url <enter>
    # (skipped, none); key storage 1 = plaintext; then the key.
    answers = ["3", "1", "", "1", "anthropic-secret"]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=lambda _msg: None,
    )

    assert written is True
    assert local_path.is_file()
    config = load_config(config_path)
    assert config.provider.name == "anthropic"
    assert config.provider.api_key == "anthropic-secret"


# --- Custom third-party branch ---


def test_wizard_custom_third_party_branch(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    # Channel 6 = custom; route name default (openai); base_url; model; key.
    answers = [
        "6",
        "",
        "https://custom.example/v1",
        "custom-model",
        "1",
        "custom-plain-key",
    ]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=lambda _msg: None,
    )

    assert written is True
    config = load_config(config_path)
    assert config.provider.name == "openai"
    assert config.provider.base_url == "https://custom.example/v1"
    assert config.provider.model == "custom-model"
    assert config.provider.api_key == "custom-plain-key"


def test_wizard_custom_branch_requires_base_url(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    output: list[str] = []
    # Channel 6 = custom; route name default; empty base_url -> abort.
    answers = ["6", "", ""]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=output.append,
    )

    assert written is False
    assert not _local_config_path(config_path).exists()
    assert any("Base URL is required" in line for line in output)


# --- Environment-variable branch ---


def test_wizard_env_branch_writes_api_key_env(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    # Channel 4 = Qwen; model 2 = qwen-max; base_url default;
    # key storage 2 = env var; env name <enter> = preset default.
    answers = ["4", "2", "", "2", ""]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=lambda _msg: None,
    )

    assert written is True
    local_text = _local_config_path(config_path).read_text(encoding="utf-8")
    assert 'api_key_env = "DASHSCOPE_API_KEY"' in local_text
    assert "api_key =" not in local_text
    config = load_config(config_path)
    assert config.provider.api_key is None
    assert config.provider.api_key_env == "DASHSCOPE_API_KEY"


# --- Cancellation / invalid input handling ---


def test_wizard_cancel_returns_false_without_writing(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)

    def _eof(_prompt: str) -> str:
        raise EOFError

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_eof,
        output_fn=lambda _msg: None,
    )

    assert written is False
    assert not _local_config_path(config_path).exists()


def test_wizard_invalid_channel_returns_false(tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    output: list[str] = []

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(["not-a-number"]),
        output_fn=output.append,
    )

    assert written is False
    assert any("Invalid choice" in line for line in output)


def test_wizard_aborts_without_corrupting_unparseable_local_config(tmp_path: Path) -> None:
    # An existing local config whose other section contains a line that is
    # literally ``[provider]`` inside a multi-line string would splice into
    # invalid TOML. The wizard must abort cleanly and leave the file untouched
    # rather than crash or corrupt it.
    config_path = _make_base_config(tmp_path)
    local_path = _local_config_path(config_path)
    original_local = '[ui]\nbanner = """\n[provider]\nnot a real table\n"""\n'
    local_path.write_text(original_local, encoding="utf-8")
    output: list[str] = []
    answers = ["1", "1", "", "1", "deepseek-secret"]

    written = run_setup_wizard(
        config_path=config_path,
        input_fn=_scripted_input(answers),
        output_fn=output.append,
    )

    assert written is False
    assert any("not be valid TOML" in line for line in output)
    # Original file preserved verbatim -- no partial/corrupt write.
    assert local_path.read_text(encoding="utf-8") == original_local


# --- _has_usable_key ---


def _provider(**overrides) -> ProviderConfig:
    base = ProviderConfig(
        name="openai",
        model="gpt-4o",
        api_key_env="OPENAI_API_KEY",
    )
    return dataclasses.replace(base, **overrides)


def test_has_usable_key_true_with_explicit_api_key() -> None:
    assert _has_usable_key(_provider(api_key="anything")) is True


def test_has_usable_key_true_with_sk_prefixed_env_field() -> None:
    assert _has_usable_key(_provider(api_key_env="sk-literal-key")) is True


def test_has_usable_key_true_when_env_var_set(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert _has_usable_key(_provider(api_key_env="OPENAI_API_KEY")) is True


def test_has_usable_key_false_when_env_var_unset(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _has_usable_key(_provider(api_key_env="OPENAI_API_KEY")) is False


def test_has_usable_key_false_with_no_key_and_no_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _has_usable_key(_provider(api_key=None, api_key_env="")) is False


# --- parse_args backward compatibility + init subcommand ---


def test_parse_args_without_subcommand_keeps_overrides() -> None:
    args = parse_args(["--provider", "openai", "--model", "gpt-4o"])

    assert args.command is None
    assert args.provider == "openai"
    assert args.model == "gpt-4o"


def test_parse_args_recognizes_init_subcommand() -> None:
    args = parse_args(["init"])

    assert args.command == "init"


def test_parse_args_init_accepts_config_flag(tmp_path: Path) -> None:
    target = tmp_path / "alt.toml"
    args = parse_args(["init", "--config", str(target)])

    assert args.command == "init"
    assert args.config == target


# --- main() dispatch ---


def test_main_init_dispatches_to_wizard(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    captured: dict[str, object] = {}

    def _fake_wizard(*, config_path: Path) -> bool:
        captured["config_path"] = config_path
        return True

    monkeypatch.setattr(main_module, "resolve_config_path", lambda _path: config_path)
    monkeypatch.setattr(main_module, "run_setup_wizard", _fake_wizard)

    assert main_module.main(["init"]) == 0
    assert captured["config_path"] == config_path


def test_main_init_returns_one_when_wizard_cancelled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(main_module, "resolve_config_path", lambda _path: tmp_path / "config.toml")
    monkeypatch.setattr(main_module, "run_setup_wizard", lambda *, config_path: False)

    assert main_module.main(["init"]) == 1


def test_main_first_run_triggers_wizard_then_reloads(monkeypatch, tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    monkeypatch.setattr(main_module, "resolve_config_path", lambda _path: config_path)
    monkeypatch.setattr(main_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls = {"wizard": 0, "load": 0}

    def _fake_wizard(*, config_path: Path) -> bool:
        calls["wizard"] += 1
        # Simulate the wizard writing a usable key into the local override.
        _local_config_path(config_path).write_text(
            '[provider]\nname = "deepseek"\napi_key = "now-usable"\n',
            encoding="utf-8",
        )
        return True

    real_load_config = main_module.load_config

    def _counting_load_config(*args, **kwargs):
        calls["load"] += 1
        return real_load_config(*args, **kwargs)

    monkeypatch.setattr(main_module, "run_setup_wizard", _fake_wizard)
    monkeypatch.setattr(main_module, "load_config", _counting_load_config)
    monkeypatch.setattr(main_module, "create_provider", lambda _config: object())
    monkeypatch.setattr(main_module, "_run_stdio_session", lambda *a, **k: 0)

    assert main_module.main([]) == 0
    assert calls["wizard"] == 1
    # Loaded once before the wizard and once after the reload.
    assert calls["load"] == 2


def test_main_first_run_skips_wizard_without_tty(monkeypatch, tmp_path: Path) -> None:
    config_path = _make_base_config(tmp_path)
    monkeypatch.setattr(main_module, "resolve_config_path", lambda _path: config_path)
    monkeypatch.setattr(main_module.sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    wizard_calls: list[int] = []
    monkeypatch.setattr(
        main_module,
        "run_setup_wizard",
        lambda *, config_path: wizard_calls.append(1) or True,
    )
    # Provider construction fails fast (no key) -> main returns 1, no wizard.
    assert main_module.main([]) == 1
    assert wizard_calls == []


def test_resolve_config_path_default_used_for_init(tmp_path: Path, monkeypatch) -> None:
    # Sanity: init with no --config resolves through the standard path logic.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAREAGENT_CONFIG", raising=False)
    assert resolve_config_path(None) == main_module.DEFAULT_CONFIG_PATH


def test_local_config_path_matches_main_local_resolution(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert _local_config_path(config_path) == tmp_path / "config.local.toml"

    nested = tmp_path / "configs" / "dev.toml"
    assert _local_config_path(nested) == tmp_path / "configs" / "dev.local.toml"


def test_setup_module_has_no_new_dependencies() -> None:
    # The wizard must remain stdlib-only (no tomlkit/tomli). Inspect the
    # imported module's namespace rather than its source text so the docstring
    # mentioning those names by way of explanation does not trip the guard.
    namespace = vars(wizard_module)
    assert "tomlkit" not in namespace
    assert "tomli" not in namespace
    # ``tomllib`` (stdlib) is the only TOML module that may be present.
    assert "tomllib" in namespace
