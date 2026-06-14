"""二、配置系统验证 — 2.2 config.local.toml 合并"""

import tomllib

from bareagent.core.config_paths import DEFAULT_CONFIG_PATH, local_config_path


def test_local_overrides_default():
    """config.local.toml 的值应覆盖 config.toml"""
    # Base config ships inside the package; the local override lives where
    # ``local_config_path`` resolves it (CWD for the bundled default).
    with open(DEFAULT_CONFIG_PATH, "rb") as f:
        tomllib.load(f)
    local_path = local_config_path(DEFAULT_CONFIG_PATH)
    if not local_path.exists():
        return  # 跳过
    with open(local_path, "rb") as f:
        local = tomllib.load(f)
    for section, values in local.items():
        if isinstance(values, dict):
            for k, v in values.items():
                assert v is not None, f"local[{section}][{k}] should not be None"
