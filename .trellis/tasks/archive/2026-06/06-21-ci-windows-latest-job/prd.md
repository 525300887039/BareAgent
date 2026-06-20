# CI 加 windows-latest job（覆盖开发主力平台）

## Goal

CI 目前只在 ubuntu-latest 跑;而开发主力是 Windows。导致 main 一周变红的那次正属「平台差异类 bug 没在 CI 暴露」。
加一个 windows-latest job,在 CI 里也覆盖开发主力平台,抓住路径/CRLF/编码/tempfile 等 Windows 专属问题。

## What I already know

- CI = `.github/workflows/ci.yml`：`test`（ubuntu，ruff + `uv run pytest`）+ `socket`（ubuntu，`-m socket`，阻塞）
  + `notify`（`needs: [test, socket]`，main 变红开/关 `ci-failure` issue，纯函数 `combine_conclusions` 归并多 job 结论）。
- **套件在 Windows 上本就通过**：本机即 Windows，默认 `pytest`（`-m 'not manual'`）刚跑 = 1227 passed / 47 deselected。
  故 windows-latest 跑默认套件大概率绿；剩余风险仅「干净 runner vs 本开发机」环境差异（首跑分支验证暴露）。
- socket 测试**不**应在 windows 跑（它们正因 Windows loopback flake 才被标 manual）——windows job 只跑默认 `-m 'not manual'`。
- memory「Windows 别用 uv run pytest」是**本 session 的 PowerShell 工具**把 stderr 判 exit 1，与 GitHub windows runner 无关
  （CI step 只看退出码，stderr 构建提示无害）。
- ruff 跨平台结果一致，无需在 windows 重复跑（跑一次即可）。

## Decisions (ADR-lite)

- **[Q1] 把 `test` job 改 `strategy.matrix.os: [ubuntu-latest, windows-latest]` + `fail-fast: false`，ruff 仅 Linux 跑**
  （`if: runner.os == 'Linux'`）。一份 job 定义覆盖两 OS、单一事实源；matrix 的 `needs.test.result` 聚合（任一 leg 失败即
  failure），故 **Q3** 自动满足（notify 的 `needs: [test, socket]` 不改，windows 失败自动并入通知）、**Q4** 自动满足
  （ruff 跨平台一致只跑一次）。guard 加断言 matrix 含 `windows-latest`。
- **[Q2] windows leg 阻塞为目标 + 分支验证 + triage 到绿；triage 失控则退非阻塞**。套件在本机(Windows)1227 passed
  是「会绿」强证据；windows-latest 干净 runner 仍属未知，branch PR 实证。triage 策略：真 bug 当场修 / 纯平台特异不值得修则
  `@pytest.mark.skipif(sys.platform=="win32", reason=...)` 标掉 / 失败多到膨胀则 windows leg 翻 `continue-on-error`
  （`${{ matrix.os == 'windows-latest' }}`）非阻塞观察 + 修复拆后续，**绝不让 main 因此变红**。

## Requirements

- `.github/workflows/ci.yml`：`test` job 加 `strategy: {fail-fast: false, matrix: {os: [ubuntu-latest, windows-latest]}}`，
  `runs-on: ${{ matrix.os }}`；Lint(ruff) 步骤加 `if: runner.os == 'Linux'`（仅 Linux 跑一次）；
  其余步骤（setup-uv / uv venv / uv pip install / uv run pytest）OS 无关照常两 leg 跑。
- `notify` 的 `needs: [test, socket]` **不改**（matrix `test` 聚合两 OS，windows 失败自动并入通知）。
- `socket` job 维持仅 ubuntu（socket 测试在 Windows loopback flake，不上 windows）。
- `tests/test_ci_visibility.py`：补 guard 断言 matrix 含 `windows-latest` 且 `fail-fast: false`。
- 分支 PR 上 windows leg 跑 `uv run pytest`（默认 `-m 'not manual'`）；按 triage 策略处理失败直至绿（或退非阻塞）。

## Acceptance Criteria

- [ ] 分支 PR：CI 的 `test (windows-latest)` leg 跑默认套件并**绿**（或按退路标记/非阻塞，且记录原因）。
- [ ] `test (ubuntu-latest)` + `test (windows-latest)` + `socket` + `notify` 全部按预期（两绿时 notify NOOP）。
- [ ] ruff 只在 ubuntu leg 跑一次（windows leg 跳过 lint）。
- [ ] 本机默认 `pytest` 行为不变；guard 测试（matrix 含 windows、fail-fast:false）全绿；ruff 干净。
- [ ] 全部稳定后才 ff-merge main。

## Definition of Done (team quality bar)

- 新增/变更行为补测试或验证；ruff 干净；Conventional Commits 大写前缀；源码禁 emoji。
- 分支 PR 先验证 windows job 在 CI 绿，再 ff-merge main（沿用 socket job 的稳妥流程）。

## Out of Scope (explicit)

- 多 Python 版本矩阵（仅 3.12）、macOS job。
- socket 测试上 windows（Windows loopback flake，刻意不跑）。
- 若 windows 暴露**大量**专属失败，深度修复拆成后续任务（本任务先用 skipif/非阻塞兜住，不阻塞落地）。
- ruff 版本钉定（推荐 #3，独立任务）、notify 失败路径真实冒烟（推荐 #4，独立任务）。

## Technical Notes

- 关键文件：`.github/workflows/ci.yml`、`scripts/ci_notify.py`（若纳入 notify）、`tests/test_ci_visibility.py`（guard）。
- 前序：06-20-ci-pre-push-main（notify + pre-push）、06-20-ci-manual-linux-job-socket-http（socket job + combine_conclusions）。
