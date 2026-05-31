from __future__ import annotations

from src.main import CostConfig, _parse_cost_config, load_config


def test_config_has_cost_default(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert isinstance(cfg.cost, CostConfig)
    assert cfg.cost.prices == {}


def test_load_config_parses_cost_prices(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        '[cost.prices."claude-opus-4-8"]\ninput = 15.0\noutput = 75.0\n\n'
        '[cost.prices."deepseek-chat"]\ninput = 0.27\noutput = 1.1\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.cost.prices["claude-opus-4-8"] == {"input": 15.0, "output": 75.0}
    assert cfg.cost.prices["deepseek-chat"] == {"input": 0.27, "output": 1.1}


def test_local_override_replaces_builtin_price(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        '[cost.prices."claude-opus-4-8"]\ninput = 15.0\noutput = 75.0\n',
        encoding="utf-8",
    )
    local_file = tmp_path / "config.local.toml"
    local_file.write_text(
        '[cost.prices."claude-opus-4-8"]\ninput = 1.0\noutput = 2.0\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.cost.prices["claude-opus-4-8"] == {"input": 1.0, "output": 2.0}


def test_parse_cost_config_empty_is_safe():
    assert _parse_cost_config({}).prices == {}


def test_parse_cost_config_skips_malformed_entries():
    raw = {
        "prices": {
            "good": {"input": 1.0, "output": 2.0},
            "missing-output": {"input": 1.0},
            "wrong-type": "not-a-dict",
            "bad-value": {"input": "abc", "output": 2.0},
        }
    }
    parsed = _parse_cost_config(raw)
    assert parsed.prices == {"good": {"input": 1.0, "output": 2.0}}
