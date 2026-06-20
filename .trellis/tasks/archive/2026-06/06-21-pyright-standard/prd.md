# pyright 类型门收紧到 standard 模式

## Goal

把刚接入 CI 的 pyright 类型门从 `typeCheckingMode="basic"` 收紧到 `"standard"`，ratchet 一批 `reportXxx`（method/variable override、optional 下标等）从 none/warning 提到 error，让这些类别的**未来回归**被门拦下。承接 task 06-21-pyright-ci（CLAUDE.md 明确列 standard 为后续扩展位）。

## 确认事实（探查所得）

- 当前 `[tool.pyright]`：`typeCheckingMode="basic"`（06-21-pyright-ci 落地，basic 下 0 error）。
- **standard 模式探查**（临时改 pyproject 跑 `uv run pyright` 后已还原）：**2 errors, 7 warnings**（warnings 同 basic，全是未装可选依赖的 `reportMissingImports`，不致红）。
- 2 个 error 都在 always-imported 代码（非可选依赖 gated）→ CI 同样出现（⊆ 逻辑同 06-21-pyright-ci：CI error 集 ⊆ 本机集）：
  1. `core/tools.py:715`：`_wrapped.__wrapped__ = handler`（recency wrapper 动态挂属性，合法；`FunctionType` stub 未声明 `__wrapped__` 可写）→ 修 `_wrapped.__dict__["__wrapped__"] = handler`（无 ignore、行为等价；注意 `setattr(x,"const",y)` 会撞 ruff B010，直接 `x.__wrapped__=` 会撞 pyright，故走 `__dict__` 两者都过）。
  2. `debug/web_viewer.py:18`：`DebugViewerHandler.server: DebugViewerServer`（把基类 `server` 注解收窄到子类，typed handler 惯用法）触发 `reportIncompatibleVariableOverride` → targeted `# pyright: ignore[reportIncompatibleVariableOverride]` + reason（spec 认可：带 code + reason 的 ignore）。

## Requirements

- `[tool.pyright]` `typeCheckingMode` 由 `basic` 改 `standard`。
- 清掉 standard 下的 2 个 error（修法见上），使 `uv run pyright` exit 0 → 门维持**阻塞**。
- 维持 `include=["src/bareagent"]`（不扩 tests）、`reportMissingImports="warning"`、pin `pyright==1.1.409` 不变。
- 防回归 guard：`tests/test_ci_visibility.py` 补断言 `typeCheckingMode = "standard"` 在 pyproject（防悄悄退回 basic）。
- CLAUDE.md「CI 可见性」(5) pyright 段补一句 standard 收紧（小幅 Docs）。

## Acceptance Criteria

- [ ] `pyproject.toml` `typeCheckingMode = "standard"`。
- [ ] `uv run pyright` 0 error（warnings 允许）。
- [ ] `core/tools.py` 经 `__dict__` 赋值修 `__wrapped__`（无 ignore）；`web_viewer.py` 有带 reason 的 targeted ignore。
- [ ] `tests/test_ci_visibility.py` 断言 standard 模式在位。
- [ ] 全套 pytest 绿；本地闸 `bash scripts/ci-check.sh` 过。
- [ ] 分支 PR CI 连续绿后 ff-merge main。
- [ ] CLAUDE.md 同步。

## Out of Scope

- pyright 扩到 tests/（独立后续）。
- 收紧到 strict（standard 已是本轮目标）。
- 全局降级任何 `reportXxx` 级别（用 targeted ignore 而非全局放松——除非 brainstorm 改判）。

## Open Questions

- Q1（见下）：error 2 用 targeted ignore vs 全局降级 `reportIncompatibleVariableOverride`。
