# T1 工具链配置固化

## 目标

把 ruff / pytest / pyright 配置固化进 `pyproject.toml`，并让 conftest 自动把不稳定/手动测试标记为 `manual+slow`，使默认 `pytest` 稳定可重复。

## 前置检查

`.venv\Scripts\ruff.exe check --select E,F,I,B,UP,W src tests` 必须全绿。若有违规，缩小 select 或 `--fix`。

## 改动

### 1. `pyproject.toml` 末尾追加

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "W"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not manual'"
markers = [
    "manual: needs manual run / external services / real sockets (excluded by default; run with -m manual)",
    "slow: long-running tests",
    "e2e: end-to-end tests needing external servers (lsp/mcp/api)",
]

[tool.pyright]
pythonVersion = "3.12"
venvPath = "."
venv = ".venv"
include = ["src"]
typeCheckingMode = "basic"
reportMissingImports = "warning"
```

### 2. `tests/conftest.py`

- 顶部加 `import pytest`。
- 末尾加 collection 钩子，把 `*_manual.py` 与 `test_web_viewer` 自动标记 `manual+slow`：

```python
def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if path.endswith("_manual.py") or "test_web_viewer" in path:
            item.add_marker(pytest.mark.manual)
            item.add_marker(pytest.mark.slow)
```

## 验收

- `.venv\Scripts\ruff.exe check src tests` 全绿。
- `.venv\Scripts\python.exe -m pytest` 默认运行 0 failed。
- `... -m pytest --collect-only -q | Out-File` 后 Grep 确认 `test_web_viewer` 出现 **0 次**（被默认排除）。
