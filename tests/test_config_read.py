"""二、配置系统验证 — 2.3 _read_config_file 函数直接测试"""

from pathlib import Path

from src.main import _read_config_file

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"


def test_read_config_file_basic():
    """_read_config_file 应返回 dict"""
    cfg = _read_config_file(CONFIG_PATH)
    assert isinstance(cfg, dict)
    assert "provider" in cfg


def test_read_config_file_nonexistent():
    """不存在的文件应抛出异常（底层直接 open）"""
    try:
        _read_config_file(Path("/nonexistent/config.toml"))
        assert False, "Should have raised"
    except (FileNotFoundError, OSError):
        pass


def test_read_config_file_invalid_toml(tmp_path):
    """非法 TOML 应抛出异常"""
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("invalid [[[toml syntax", encoding="utf-8")
    try:
        _read_config_file(bad_file)
        assert False, "Should have raised"
    except Exception:
        pass
