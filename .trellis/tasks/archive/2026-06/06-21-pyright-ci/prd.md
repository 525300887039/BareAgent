# pyright 类型检查进 CI（配了却没强制执行的门）

## Goal

把已配置但从未执行的 pyright 类型检查接进 CI 与本地 push 前闸，补上「配了却没强制执行」的质量门——同 ruff 已做的那样。消除「检查存在但没在该执行的地方执行」这一与 2026-06「main 红一周无人察觉」同源的失效模式。

## 确认事实（探查所得，不需再问）

- pyproject 已有 `[tool.pyright]`：`pythonVersion=3.12, venvPath=".", venv=".venv", include=["src/bareagent"], typeCheckingMode="basic", reportMissingImports="warning"`。但 `.github/workflows/ci.yml` 从不跑 pyright。
- 现有 CI：job `test`（matrix os=[ubuntu, windows], fail-fast:false；Lint+format 步骤 `if: runner.os=='Linux'` 跑 ruff check + format --check；Test 步骤 `uv run pytest`）+ job `socket`（ubuntu，`-m socket`）+ job `notify`（needs:[test,socket]，main 变红开/关 ci-failure issue）。
- 本地闸 `scripts/ci-check.sh`（被 `.githooks/pre-push` 复用）跑 ruff check + format --check + pytest（全 `uv run`）。
- 防回归 guard 在 `tests/test_ci_visibility.py`（纯函数单测 + 静态断言）。
- **探查报数**：`uv pip install pyright`（PyPI=node pyright wrapper，自动下 node 运行时）→ 装得 `pyright==1.1.409` → `uv run pyright` 得 **10 errors, 7 warnings, exit 1**。
  - 7 warnings 全是可选依赖缺失（fastembed/opentelemetry 的 `reportMissingImports`）→ warning 级、**不**致 CI 红（exit code 只由 error 决定）。符合预期。
  - 10 errors 全部可干净修复的真错（narrowing 类，**无需** `# pyright: ignore`、**无需**放松 `reportXxx` 级别）：
    - `main.py:2952`（×2，reportArgumentType）：`int(tool_budget)`，narrowing 藏在单独布尔变量里 → 内联 isinstance if-block。
    - `code_index.py:215/233/241` + `persistent.py:401/412/417`（×6，reportOptionalMemberAccess）：`self._embedder`（`Embedder|None`）跨方法 narrowing 丢失 → 方法首行 `assert self._embedder is not None`（2 处 assert）。
    - `repo_map.py:191`（reportOptionalMemberAccess）：`personalization` `total_p>0` 隐含非 None → 分支内 assert。
    - `repo_map_extract.py:144`（reportOptionalMemberAccess）：tree-sitter `Node.text` 是 `bytes|None` → `(... or b"").decode()`。
  - 全部修复行为保持或更安全。

## Requirements

- pyright pin exact `pyright==1.1.409`（探查/triage 基线版本，可复现、防新版突袭）进 `[project.optional-dependencies] dev`。
- CI 跑 pyright：加进现有 `test` job 的 Linux-gated 步骤（与 ruff 同档，类型检查平台无关只跑一次；自动并入 `needs.test.result` → notify 已覆盖，零额外接线）。用 `uv run pyright`。
- 本地闸：`scripts/ci-check.sh` 加 `uv run pyright`（与 CI 忠实一致，pre-push 即拦类型错）。
- triage 10 个 error 为真修复（见上表），使 pyright exit 0 → 类型检查**阻塞**（error 致 CI 红）。
- 维持 `[tool.pyright]` 现状：`include=["src/bareagent"]`（不扩到 tests）、`typeCheckingMode="basic"`（本任务不收紧到 standard）。
- 防回归 guard：`tests/test_ci_visibility.py` 补静态断言（ci.yml 与 ci-check.sh 含 pyright、pyright 已 exact pin）。
- 收尾 `CLAUDE.md`「## CI 可见性」小节同步加 pyright 段（对齐既有 (1)(2)(3)(4) 编号风格，Docs commit）。

## Acceptance Criteria

- [ ] `uv run pyright` 在 src/bareagent 上 0 error（warnings 允许，来自未装可选依赖）。
- [ ] `.github/workflows/ci.yml` 的 `test` job Linux-gated 步骤含 `uv run pyright`。
- [ ] `scripts/ci-check.sh` 含 `uv run pyright`。
- [ ] `pyproject.toml` dev extra 含 `pyright==1.1.409`（exact pin，无 `pyright>=`）。
- [ ] `tests/test_ci_visibility.py` 新增断言覆盖：ci.yml 含 pyright、ci-check.sh 含 pyright、pyright exact pin。
- [ ] 全套 pytest 绿（含新增 guard）；本地闸 `bash scripts/ci-check.sh` 通过。
- [ ] 分支 PR CI 连续绿后 ff-merge main（沿用近期 CI 系列稳妥流程）。
- [ ] `CLAUDE.md`「## CI 可见性」补 pyright 段。

## Out of Scope

- 扩 pyright 范围到 tests/。
- 收紧 `typeCheckingMode` 到 standard/strict。
- 把 pyright 单开成独立 CI job（并进现有 test job 即可）。
- 用 CI-only 的 GitHub Action 跑 pyright（本地闸无法复刻）。
- pyright 在 Windows leg 也跑（类型检查平台无关，Linux 一次足够）。
- branch protection / PR 强制（单人仓库刻意低摩擦，沿用现状）。

## Open Questions

- 见下方 brainstorm 逐条敲定。
