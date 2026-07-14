from __future__ import annotations

import pytest

from bareagent.main import _parse_capabilities_config


def test_absent_image_in_is_none_auto():
    cfg = _parse_capabilities_config({})
    assert cfg.image_in is None


def test_explicit_true_false_kept():
    assert _parse_capabilities_config({"image_in": True}).image_in is True
    assert _parse_capabilities_config({"image_in": False}).image_in is False


def test_malformed_value_falls_back_to_none():
    assert _parse_capabilities_config({"image_in": "yes"}).image_in is None
    assert _parse_capabilities_config({"image_in": 1}).image_in is None


@pytest.mark.parametrize("raw", ["1", "true", "on", "YES"])
def test_env_forces_true(monkeypatch, raw):
    monkeypatch.setenv("BAREAGENT_MODEL_IMAGE_IN", raw)
    # env overrides config, even a config False.
    assert _parse_capabilities_config({"image_in": False}).image_in is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "NO"])
def test_env_forces_false(monkeypatch, raw):
    monkeypatch.setenv("BAREAGENT_MODEL_IMAGE_IN", raw)
    assert _parse_capabilities_config({"image_in": True}).image_in is False


def test_env_unparseable_leaves_config_value(monkeypatch):
    monkeypatch.setenv("BAREAGENT_MODEL_IMAGE_IN", "banana")
    assert _parse_capabilities_config({"image_in": True}).image_in is True
    assert _parse_capabilities_config({}).image_in is None
