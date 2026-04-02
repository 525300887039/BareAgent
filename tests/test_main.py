from pathlib import Path

from src.main import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


def test_resolve_config_path_uses_bundled_config_outside_project_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAREAGENT_CONFIG", raising=False)

    assert resolve_config_path(None) == DEFAULT_CONFIG_PATH


def test_resolve_config_path_prefers_environment_override(monkeypatch) -> None:
    override = Path("custom-config.toml")
    monkeypatch.setenv("BAREAGENT_CONFIG", str(override))

    assert resolve_config_path(None) == override


def test_load_config_uses_matching_default_api_key_env_for_provider_override(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "anthropic"',
                'model = "claude-sonnet-4-20250514"',
                'api_key_env = "ANTHROPIC_API_KEY"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, provider_override="openai")

    assert config.provider.name == "openai"
    assert config.provider.api_key_env == "OPENAI_API_KEY"
    assert config.provider.wire_api is None


def test_load_config_reads_provider_wire_api(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                '[provider]',
                'name = "openai"',
                'model = "gpt-5-codex-mini"',
                'api_key_env = "OPENAI_API_KEY"',
                'base_url = "https://right.codes/codex/v1"',
                'wire_api = "responses"',
                "",
                '[permission]',
                'mode = "default"',
                "",
                '[ui]',
                'stream = true',
                'theme = "dark"',
                "",
                '[thinking]',
                'mode = "adaptive"',
                'budget_tokens = 10000',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider.base_url == "https://right.codes/codex/v1"
    assert config.provider.wire_api == "responses"
