"""二、配置系统验证 — 2.3 _read_config_file 函数直接测试"""

from pathlib import Path

from bareagent.core.config_paths import DEFAULT_CONFIG_PATH
from bareagent.main import _read_config_file

# config.toml ships inside the package (src/bareagent/config.toml).
CONFIG_PATH = DEFAULT_CONFIG_PATH


def test_read_config_file_basic():
    """_read_config_file 应返回 dict"""
    cfg = _read_config_file(CONFIG_PATH)
    assert isinstance(cfg, dict)
    assert "provider" in cfg


def test_read_config_file_nonexistent():
    """不存在的文件应抛出异常（底层直接 open）"""
    try:
        _read_config_file(Path("/nonexistent/config.toml"))
        raise AssertionError("Should have raised")
    except (FileNotFoundError, OSError):
        pass


def test_read_config_file_invalid_toml(tmp_path):
    """非法 TOML 应抛出异常"""
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("invalid [[[toml syntax", encoding="utf-8")
    try:
        _read_config_file(bad_file)
        raise AssertionError("Should have raised")
    except Exception:
        pass
