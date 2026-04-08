"""二、配置系统验证 — 2.1 config.toml 解析"""
import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"


def test_config_toml_valid():
    """config.toml 必须是合法 TOML"""
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    assert "provider" in cfg
    assert "permission" in cfg
    assert "ui" in cfg
    assert "thinking" in cfg


def test_config_toml_provider_fields():
    """provider 段必须包含 name, model, api_key_env"""
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    p = cfg["provider"]
    assert "name" in p
    assert "model" in p
    assert "api_key_env" in p


def test_config_toml_keeps_debug_logging_opt_in():
    """bundled config.toml 不应默认开启完整 debug 日志"""
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    assert cfg["debug"]["enabled"] is False
