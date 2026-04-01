from pathlib import Path

from src.main import DEFAULT_CONFIG_PATH, resolve_config_path


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
