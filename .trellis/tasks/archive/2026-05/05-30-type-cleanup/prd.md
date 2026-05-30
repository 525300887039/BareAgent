# T4 类型错误清理

## 目标

用 T1 加好的 `[tool.pyright]`，把 pyright error 降到 0（或记录无法消除的残留）。

## 运行方式

`.venv\Scripts\pyright.exe`（从项目根运行，让它读到 pyproject 的 `[tool.pyright]`；**不要** `-m pyright` 无参，那样读不到配置）。

## 待修区域（约 30 个真实 error）

- **`src/main.py`（最多）**：messages/handlers 的 `dict→Mapping`、`list→Sequence` 协变；裸 `str` 传 `Literal[...]` 形参需校验/cast。
- **`src/provider/factory.py`**：同类 `Literal` 收窄。
- **`src/lsp/tools.py`、`src/lsp/coord.py`**：multilspy 返回值被推断为 `object`，需显式标注/cast。
- **`src/core/handlers/bash.py`**：subprocess 输出 `bytes→str`（可能缺 `decode` / `text=True`）。

## 注意

- otel/langfuse 的 `reportMissingImports` 是可选依赖未装的误报，已被 `[tool.pyright]` 降级为 warning，不用管。
- 修复以最小侵入为原则：优先精确类型标注 / `cast`，避免改变运行时行为。

## 验收

- pyright error 数 = 0（或在 task notes 记录无法消除的残留及原因）。
- `.venv\Scripts\ruff.exe check src tests` + 默认 `pytest` 仍全绿。
