# 实施计划：发布链路与 v0.2.0

## 1. 启动与依赖复核

- [x] 确认 `07-21-fix-session-tree-cycles` 已通过、独立 commit 并完成 Trellis 收尾。
- [x] 仅在上述条件和用户实施确认均满足后 start 本任务。
- [x] 复核 `main`、未知脏文件、`HEAD...origin/main`、HEAD CI。
- [x] 再查本地/远端 `v0.2.0` 和 PyPI `bareagent-cli==0.2.0`；任一存在则停止。
- [x] 读取本任务 PRD/design/research、backend quality spec 和现有 CI visibility tests。

## 2. 测试先行：发布契约

- [x] 新增 strict tag / expected version / dist inventory 的 stdlib helper 行为测试。
- [x] 新增 release workflow 静态契约测试，先覆盖六类用户指定回归。
- [x] 增加 immutable external action refs 和 sdist layout 保护。
- [x] 运行定向测试并确认它们能识别当前 workflow 缺口，而不是因解析脆弱误报。

## 3. 可复用 CI

- [x] 提取只读 reusable `quality.yml`，由 `.github/workflows/ci.yml` 与 release 共用；保留 main push、PR、Windows matrix、socket 和 main-only notify 行为。
- [x] 将 CI 内第三方 actions 固定到研究记录的 commit SHA + 版本注释。
- [x] 保持 Ruff、format、Pyright、默认 pytest 和 socket pytest 命令不变，不放宽条件。

## 4. Release 构建与安全 DAG

- [x] 实现 `scripts/release_contract.py`：严格 tag、wheel/sdist inventory、元数据版本一致性、tag expected version。
- [x] 实现 `scripts/smoke_installed_package.py`：import、config 和三个内置 skill 资源验证。
- [x] 重构 `.github/workflows/release.yml`：
  - [x] job-level 调用 reusable `ci.yml`；
  - [x] concurrency 同 ref 串行且不 cancel；
  - [x] clean dist 后 `uv build`；
  - [x] exact wheel/sdist inventory + hatch-vcs version 验证；
  - [x] twine check 精确 globs；
  - [x] artifact 只上传本次 wheel/sdist；
  - [x] fresh wheel smoke；
  - [x] fresh sdist smoke；
  - [x] PyPI/TestPyPI publish 均显式 needs 四个前置 jobs；
  - [x] job-level 最小权限，只有 publish 有 `id-token: write`；
  - [x] 所有外部 actions 固定 SHA；
  - [x] TestPyPI 不用 `skip-existing` 掩盖重复版本。
- [x] 在 `pyproject.toml` sdist include 中补 `.github/workflows/release.yml`（及测试实际需要的布局文件）。

## 5. 版本说明与文档

- [x] 新增 `CHANGELOG.md`，主题化汇总 v0.1.0 到 v0.2.0，包括本次 session tree 修复。
- [x] 更新 `docs/releasing.md`：PR/main CI、release quality/build/smoke DAG、TestPyPI、正式 tag/PyPI、失败/重试/不可覆盖。
- [x] 更新 README 开发段落：默认 pytest 与 socket 分离、Ruff lint/format check、Pyright、docs build；不改 ROADMAP。
- [x] 不写静态版本号到 `pyproject.toml` 或包源码。

## 6. 定向与完整本地验证

- [x] `uv run pytest tests/test_release_workflow.py tests/test_ci_visibility.py`（按最终文件名调整）。
- [x] `uv run pytest tests/test_session_tree.py`，确认前一子任务不回归。
- [x] `uv run ruff check src tests`。
- [x] `uv run ruff format --check src tests`。
- [x] `uv run pyright`。
- [x] `uv run pytest`，记录 passed/deselected。
- [x] `uv run pytest -m socket`，记录 passed。
- [x] 在 `docs` 目录运行 `npm run docs:build`。
- [x] 清空 `dist`，本地构建一个 wheel + 一个 sdist，运行 pinned/选定 twine check 与 release inventory/version helper。
- [x] 在全新临时 venv 从本地 wheel 安装并运行 resource smoke + `bareagent --help`。
- [x] 在另一个全新临时 venv 从本地 sdist 构建/安装并运行同样 smoke。
- [x] `git diff --check`、完整 diff/status、确认无旧 dist 或未知文件进入提交。

## 7. 发布加固提交与远端验证

- [x] 评估并完成必要 Trellis spec 更新。
- [x] 按 Trellis Phase 3.4 展示一次提交计划；session tree 提交不得混入。
- [x] 形成发布加固提交 `61f099f5f81a06c001bf7b433bf45a85d724fe69`、权限链修复 `3c3995635b878310af3474e7eb7f592985708673` 与 TestPyPI 安装文档修正 `b4ada030712ac1055626a0bddfe14fa769111851`。
- [x] 记录 commit SHA/验收元数据，保持任务 in_progress 直到发布完成。
- [x] push `main`/目标提交并等待该 SHA 的 GitHub CI 成功。
- [x] 触发 release workflow `workflow_dispatch` 到 TestPyPI；观察所有 quality/build/smoke/publish jobs。
- [x] 获取最终 dev version `0.1.1.dev72`，从 TestPyPI 精确 wheel URL 安装、由 PyPI 解析依赖，运行 import/help/resource/pip-check smoke。
- [x] 首次 TestPyPI startup failure 后修复权限链、增加回归并重跑完整检查和 CI；未手动上传，未使用 `skip-existing`。

## 8. Release candidate 门

- [x] 再次确认工作区、diff、提交历史、main/目标 SHA 已 push 且 CI 成功。
- [x] 再查本地 tag、远端 tag、GitHub release、PyPI/TestPyPI 的 `0.2.0` 占用。
- [x] 汇报最终 commit SHA、六项本地质量结果、构建/安装结果、CI/TestPyPI URL、待创建 `v0.2.0`、publish DAG 与剩余风险。
- [x] 只请求一次最终发布确认，然后等待；不得预创建 tag。

## 9. 最终发布（仅确认后）

- [x] 确认 HEAD 等于已报告 SHA，创建 annotated `v0.2.0` tag。
- [x] 验证 tag annotation/target 后 push 单个 tag。
- [x] 持续观察 tag 对应 release run 到 success 或明确 failure；不把等待 environment approval 误报为成功。
- [x] 验证 PyPI 项目页、`0.2.0` JSON、wheel/sdist 文件与版本。
- [x] 在第三个全新环境从正式 PyPI 安装 `bareagent-cli==0.2.0`，运行 import、resource smoke、`bareagent --help`。
- [x] 汇报 PyPI URL、workflow URL/结论、安装验证；失败则报告不可变版本事实和后续修复策略。

## 10. Trellis 收尾

- [x] 更新 task acceptance、commit、tag、release/workflow URL 和已知风险元数据。
- [x] 完成本任务与父任务归档、session journal；检查最终 git status 和历史。

## 回滚点

- tag push 之前所有 workflow/script/doc 变更均可通过新提交修正；不 rewrite 用户历史。
- tag push 之后禁止移动/删除/复用 `v0.2.0`；发布失败使用诊断 + 后续新版本策略。

## 本地验证记录（2026-07-22）

- 发布契约与 CI visibility：`69 passed`；session tree 回归：`30 passed`。
- Ruff lint / format：项目门与发布 helpers 定向门均通过。
- Pyright：项目 `0 errors`、7 个既有可选依赖 warnings；发布 helpers/tests `0 errors, 0 warnings`。
- 默认 pytest：`1432 passed, 47 deselected`；socket：`11 passed, 1468 deselected`。
- VitePress docs build 与 `git diff --check`：通过。
- 修复后干净构建版本：`0.1.1.dev70`；`dist/` 恰有一个 wheel + 一个 sdist，twine check 均通过，sdist 含三个 workflow 文件。
- wheel 与 sdist 分别在 Python `3.12.13` 全新 venv 安装；import、`config.toml`、三个内置 skills、`bareagent --help` 全部通过。
- 本地/远端 `v0.2.0` tag 不存在；PyPI/TestPyPI `0.2.0` JSON 均为 404。
- 已知范围风险：独立 `deploy-docs.yml` 不属于 CI/PyPI 发布 DAG，本任务未 pin 其可移动 action refs；CI 与 PyPI release 链路已全部固定 SHA。

## 正式发布记录（2026-07-22）

- 用户给出单次最终发布确认后，创建 annotated `v0.2.0`，并验证其 peeled commit 为 `b4ada030712ac1055626a0bddfe14fa769111851` 后只推送该 tag。
- tag-triggered release run `29885666639` 成功；quality、build、wheel smoke、sdist smoke 和 PyPI Trusted Publishing jobs 全部通过。
- PyPI `0.2.0` 元数据恰有 `bareagent_cli-0.2.0-py3-none-any.whl` 与 `bareagent_cli-0.2.0.tar.gz`。
- Python `3.12.13` 全新 venv 从正式 PyPI 安装 `bareagent-cli==0.2.0`；版本、import、运行时资源、`bareagent --help` 与 `uv pip check` 全部通过。
