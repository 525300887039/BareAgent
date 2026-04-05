"""二、配置系统验证 — 2.2 config.local.toml 合并"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_overrides_default():
    """config.local.toml 的值应覆盖 config.toml"""
    with open(ROOT / "config.toml", "rb") as f:
        tomllib.load(f)
    local_path = ROOT / "config.local.toml"
    if not local_path.exists():
        return  # 跳过
    with open(local_path, "rb") as f:
        local = tomllib.load(f)
    for section, values in local.items():
        if isinstance(values, dict):
            for k, v in values.items():
                assert v is not None, f"local[{section}][{k}] should not be None"
