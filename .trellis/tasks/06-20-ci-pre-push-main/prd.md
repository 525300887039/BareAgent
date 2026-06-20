# CI 可见性：pre-push 检查 + main 变红通知

## Goal

让「CI 会失败」在更早、更显眼的环节被捕获/通知，避免 main 再次长期变红而无人察觉。
真实事件：main CI 从 ~2026-06-14 红到 06-20（一周多无人发现），根因（test 的 `from tests.conftest import` 在
`uv run pytest` 下 ModuleNotFoundError）虽已用 `pythonpath=["."]` 修好，但**元问题**未解：
(1) 本机习惯用 `python -m pytest`（前插 cwd 到 sys.path）掩盖了 CI 裸 `uv run pytest` 才暴露的差异；
(2) 没有任何「push 前」或「main 变红时」的提醒机制。

## What I already know

- CI = `.github/workflows/ci.yml`：`push:[main]` + `pull_request`，单 job `test`，步骤
  `uv venv` → `uv pip install -e ".[dev]"` → `uv run ruff check src tests` → `uv run pytest`。
- 零现有 hooks 基建：无 `.githooks/`、无 `core.hooksPath`、无 `scripts/`、无 Makefile。
- pyproject 已含根因修复：`[tool.pytest.ini_options] pythonpath=["."]` + `addopts="-m 'not manual'"`
  + markers（manual/slow/e2e）。默认 `pytest` 跑 512 passed / 46 deselected。
- 本机习惯命令是 `.venv\Scripts\python.exe -m pytest`（= `python -m pytest`），正是掩盖 bug 的元凶。
- 仓库习惯**直接往 main 推** chore 提交（journal / task archive），不全走 PR。
- 开发主力 Windows（PowerShell + git-bash 都在用），CI 是 Linux。
- release.yml 显示仓库 GH Actions 风格：OIDC / environments / 干净 YAML。

## Candidate solutions (待 brainstorm 取舍)

1. 受版本控制的 pre-push git hook（`.githooks/` + `core.hooksPath`）：push 前跑 CI 等价检查
   （**裸 `uv run pytest` 复刻 CI sys.path**，非 `python -m pytest`）。可被 `--no-verify` 绕过、需主动装、要跨平台。
2. main 变红即通知的 GH Actions：main CI 失败时自动开/更新 GitHub issue。事后兜底，保证「红了一定被看见」。
3. branch protection：强制 CI 通过才进 main。服务端不可绕，但与「直接推 main」习惯冲突。
4. 本地一键跑 CI 同款检查的脚本（被 hook 复用，也供手动跑）。

## Decisions (ADR-lite)

- **[Q1] 不采纳方案3（强制 PR / branch protection）**。Context: 单人仓库、大量 chore 直接推 main 是刻意的低摩擦工作流。
  Decision: 保留直接推 main，靠方案 1（push 前本地闸）+ 方案 2（main 变红通知）一前一后兜住。
  Consequences: 不动现有推送习惯；本地闸可被 `--no-verify` 绕过，故必须有方案 2 服务端兜底保证「红了一定被看见」。
- **[Q2] MVP = 方案 4（脚本）+ 1（hook）+ 2（通知）三件全做**，分层依赖一次实现。脚本是地基被 hook 复用，通知独立兜底。
- **[Q3] 脚本完整复刻 CI（`uv run ruff check src tests` + `uv run pytest`），hook 默认跑全套 + 可跳过旋钮**。
  口径必须 `uv run`（元问题解药：本机 `python -m pytest` 前插 cwd 掩盖了 CI 裸 `uv run` 暴露的 sys.path 差异）。
  `uv` 缺失 → fail-closed 清晰报错 + 提示跳过旋钮。逃生口：`BAREAGENT_PREPUSH_SKIP=1` 环境变量 + git 原生 `--no-verify`。
- **[Q4] main 变红通知 = 失败开/复用单一 issue + 恢复自动关，全闭环，标题纯文字无 emoji**。
  实现放 ci.yml 内 `notify` job（`needs: test`），避免 `workflow_run` 跨 workflow 权限/上下文坑。
  去重靠固定 label `ci-failure`（幂等建 label）。决策逻辑抽成纯函数 `scripts/ci_notify.py:decide_action` 注入式可单测。

## Requirements

### 方案4 — 本地 CI 等价脚本（地基）
- `scripts/ci-check.sh`（bash，git-bash 可跑）：依次跑 `uv run ruff check src tests` + `uv run pytest`，任一失败即非零退出。
- `uv` 不存在 → 清晰报错（提示装 uv 或用跳过旋钮）并非零退出。
- 供 hook 复用，也供手动 `bash scripts/ci-check.sh` 跑。

### 方案1 — pre-push git hook（push 前本地闸）
- `.githooks/pre-push`（committed，`#!/usr/bin/env bash`）：调 `scripts/ci-check.sh`，失败拦 push。
- 跳过旋钮：`BAREAGENT_PREPUSH_SKIP=1` 时打印一行提示并放行（叠加 git 原生 `--no-verify`）。
- `scripts/setup-hooks.sh`：一次性设 `git config core.hooksPath .githooks`（committed hook 不能自动生效，需主动装一次）。
- README 文档化：怎么装、怎么跳过、为什么用 `uv run`。

### 方案2 — main 变红通知（服务端兜底）
- ci.yml 加 `notify` job（`needs: test`，`if: always() && github.ref == 'refs/heads/main'`，`permissions: issues: write`）。
- `scripts/ci_notify.py`：纯函数 `decide_action(open_issues, conclusion) -> Action`（CREATE / COMMENT / CLOSE / NOOP），
  thin `main()` 用 `gh` CLI 查 open `ci-failure` issue 并 dispatch；失败→无则开/有则评论，成功→有则评论恢复+关闭。
- issue 标题纯文字（如 `CI failing on main`），正文带 commit SHA + run 链接；去重靠固定 label `ci-failure`（幂等建）。

### 防回归 guard
- `tests/test_ci_visibility.py`：静态断言 ——
  ci.yml 用 `uv run pytest`（非 `python -m pytest`）；pre-push hook 存在且调脚本/用 `uv run`；
  pyproject 保留 `pythonpath=["."]`；`ci_notify.decide_action` 的 4 分支决策表单测。

## Acceptance Criteria

- [x] `bash scripts/ci-check.sh` 在本机 git-bash 跑通，复刻 CI（ruff + uv run pytest）= 1213 passed / 47 deselected / exit 0；uv 缺失 fail-closed（exit 127）。
- [x] hook 跳过旋钮验证：`BAREAGENT_PREPUSH_SKIP=1 bash .githooks/pre-push` 打印跳过 + exit 0 不跑测试；叠加 git 原生 `--no-verify`。
- [x] ci.yml `notify` job（`needs: test`，`always() && push && refs/heads/main`，`issues: write`）调 `ci_notify.py`；决策逻辑 `decide_action` 10 参数化单测覆盖 CREATE/COMMENT/CLOSE/NOOP 全分支。
- [x] `tests/test_ci_visibility.py` 全绿（15 passed）；纯静态读文件 + 纯逻辑，不依赖网络/socket。
- [x] ruff check 改动文件干净（StrEnum + 拆长行）；README 「本地 CI 闸」小节有安装/手动跑/跳过说明。
- [x] 跨平台硬化：`.gitattributes` 窄范围钉 `*.sh` + hook 为 LF（防 core.autocrlf 注入 `\r` 破 Linux CI）；exec 位 100755。

## Definition of Done (team quality bar)

- 新增行为补 pytest（纯逻辑模块注入式可单测）。
- ruff check 干净（只 format 改动文件，别全树）。
- Conventional Commits 大写前缀；源码禁 emoji。
- 跨平台：hook/脚本 Windows（git-bash）可跑。

## Out of Scope (explicit)

- 方案3：强制 PR / branch protection（[Q1] 已砍）。
- Slack / 邮件 / 其他通知渠道（仅 GitHub issue）。
- PR（非 main）CI 失败也通知（PR 失败作者本就可见，仅 main 共享状态需 issue）。
- pre-commit hook 变体 / PowerShell `.ps1` hook（git hook 走 sh，bash 足够；PowerShell 用户直接 `git config core.hooksPath .githooks`）。
- hook 按 push 目标分支区别跑全套/子集（MVP 一律全套，靠跳过旋钮处理例外）。
- `workflow_run` 跨 workflow 解耦通知、定时重检、1h 写溢价类精细化（后续扩展位）。

## Technical Notes

- 关键文件：`.github/workflows/ci.yml`、`pyproject.toml`、（新）`.githooks/`、（新）`scripts/`。
- 根因修复 commit：`2a8fbee Chore(ci): pytest pythonpath="."`。
