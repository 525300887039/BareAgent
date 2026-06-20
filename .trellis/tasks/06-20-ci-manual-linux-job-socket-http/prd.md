# CI 跑 manual 测试的 Linux job（捡回 socket/http 零覆盖）

## Goal

给 CI 补一个 Linux job,跑当前在任何地方都没自动化覆盖的 localhost-socket 测试
（web_viewer + MCP http transport）。这些测试本机因 loopback 环境问题被标 `manual` 永久跳过,
CI 默认也是 `-m 'not manual'`,于是 = 零自动化覆盖;而它们恰恰在 Linux 上能稳跑。

## What I already know

- CI = `.github/workflows/ci.yml`：job `test`（uv venv → install → ruff → `uv run pytest`，
  默认 `-m 'not manual'`）+ 上一任务新增的 `notify` job（`needs: test`，main 变红开/关 issue）。
- `tests/conftest.py:pytest_collection_modifyitems` 给三类测试打 `manual`+`slow`：
  (1) `*_manual.py` 文件、(2) `test_web_viewer`、(3) 用 socket fixture（json_server/sse_server/legacy_server）的测试。
- **关键**：`-m manual` 是个混合桶。
  - **目标子集（纯 localhost socket，无外部依赖，Linux 能跑）**：`test_web_viewer.py`(7)
    + `test_mcp_transport_http_streamable.py`(2) + `test_mcp_transport_http_legacy.py`(2) ≈ 11 个。
  - **非目标子集（`*_manual.py`，11 文件）**：provider/lsp_e2e/mcp_e2e/config 等需 **API key / 外部 server /
    真子进程**，CI 无 key 会失败——**不能**纳入。
- 本机 memory：这些 socket 测试在这台 Windows 机器 flake（loopback/端口/超时），非产品回归。
- 上一任务刚加了「main 变红即开 issue」的 notify 机制——本任务的新 job 若 fail 在 main 上,
  是否要触发同一通知,需决策（`notify` 当前只 `needs: test`）。


## Decisions (ADR-lite)

- **[Q1] 新增专用 `socket` pytest marker，CI 跑 `pytest -m socket`**。conftest 在「web_viewer(按路径) +
  用 socket fixture」两支额外 `add_marker(socket)`（`*_manual.py` 不加，已验证零重叠）；pyproject `markers` 登记 `socket`。
  本机默认 `-m 'not manual'` 行为不变（这批仍带 manual）。声明式、抗漂移（新 socket 测试自动获标）。
- **[Q2] 阻塞（blocking）+ 分支先验证**。非阻塞=覆盖剧场（失败没人看见）；Linux loopback 本就稳（flake 是本机
  Windows 专属）。实现走分支：推分支看 socket job 在 CI 连续绿 → 才 ff-merge main；分支阶段 flake 则当场改判。
- **[Q3] socket job 纳入 notify 的 needs，任一 job 失败开 issue**。socket job 阻塞=失败即「main 红」，
  notify 只盯 test 会留盲点。实现：`notify: needs: [test, socket]` + 纯函数 `combine_conclusions([...]) -> str`
  （任一 failure→failure / 全 success→success / 否则空=NOOP）补单测，`main()` 收多 `--conclusion` 归并喂 decide_action；
  issue 正文带失败 job 名便于定位。
- **[Q4] 同一 ci.yml 加 `socket` job**（被 Q3 决定）。GH Actions `needs` 不能跨 workflow（跨需 `workflow_run` 更复杂），
  既然 notify 依赖 socket，三 job 必须同 workflow；也与仓库现状一致（ci.yml 已含 test + notify）。

## Requirements

- `tests/conftest.py`：`pytest_collection_modifyitems` 给「web_viewer(按路径) + 用 socket fixture」两支
  额外 `add_marker(pytest.mark.socket)`；`*_manual.py` 那支**不加**。本机默认 `-m 'not manual'` 行为字节级不变。
- `pyproject.toml [tool.pytest.ini_options] markers`：登记 `socket: localhost-socket tests ...`。
- `.github/workflows/ci.yml`：加 `socket` job（ubuntu-latest，uv venv + install `.[dev]`，`uv run pytest -m socket`，
  阻塞，与 test 同触发条件 push+PR）；`notify` 改 `needs: [test, socket]`，run 步骤传 `--conclusion`(test)
  与 `--conclusion`(socket) 两个值。
- `scripts/ci_notify.py`：新增纯函数 `combine_conclusions(results: list[str]) -> str`（任一 failure→failure /
  全 success→success / 否则 ""=NOOP）；`main()` 把 `--conclusion` 改 `nargs`/可多次，归并后喂 `decide_action`；
  issue 失败正文带是哪个 job 红的。
- `tests/test_ci_visibility.py`：补 `combine_conclusions` 参数化单测 + 静态 guard（ci.yml 有 `socket` job、
  跑 `-m socket`、`notify` 的 needs 含 socket）。

## Acceptance Criteria

- [ ] 分支推上去：CI 的 `socket` job 跑 `uv run pytest -m socket` 且**连续绿**（稳定性确认）。
- [ ] `test` + `socket` + `notify` 三 job 都通；notify 在两 job 皆绿时落 NOOP。
- [ ] 本机 `pytest`（默认）行为不变：socket 测试仍默认跳过（`-m 'not manual'`），deselected 数不变。
- [ ] `pytest -m socket` 在 Linux 选中且只选中 web_viewer + mcp http transport（不含需 key 的 `*_manual.py`）。
- [ ] `combine_conclusions` 单测 + guard 全绿；ruff 干净。
- [ ] 全部确认稳定后才 ff-merge main。

## Definition of Done (team quality bar)

- 新增/变更行为补测试或验证；ruff 干净；Conventional Commits 大写前缀；源码禁 emoji。
- 跨平台：marker/选择机制本机（Windows）默认行为不变（socket 测试仍默认跳过）。

## Out of Scope (explicit)

- `*_manual.py` 中需 API key / 外部 server / 真子进程的测试（provider/lsp_e2e/mcp_e2e/config 等）——不纳入 CI。
- windows-latest CI job（独立后续任务，推荐 #2）。
- ruff 版本钉定（独立后续任务，推荐 #3）。
- socket 测试本身的稳定性/超时重构、pytest-rerunfailures 等重试依赖。
- notify 失败路径的真实冒烟（推荐 #4，另行）。

## Technical Notes

- 关键文件：`.github/workflows/ci.yml`、`tests/conftest.py`、`pyproject.toml [tool.pytest.ini_options] markers`。
- 上一任务（06-20-ci-pre-push-main）：notify job + pre-push 闸已上线。
