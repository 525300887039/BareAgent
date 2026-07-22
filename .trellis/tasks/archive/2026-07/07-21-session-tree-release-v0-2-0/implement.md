# 总体实施计划

## Gate 0：规划审阅（当前阶段）

- [x] 检查 `main`、工作区、远端、历史、tag、PyPI/TestPyPI 和最近 GitHub CI 基线。
- [x] 创建父任务与两个规划态子任务。
- [x] 完成三组 PRD/design/implement 和研究材料。
- [x] 用户审阅并明确确认进入实施。
- [x] 在确认前不运行 `task.py start`，不修改产品代码/workflow，不提交、不打 tag、不发布。

## Gate 1：子任务一

- [x] `python ./.trellis/scripts/task.py start 07-21-fix-session-tree-cycles`。
- [x] 按子任务一计划实现最小 session tree 修复和回归测试。
- [x] 运行相关测试及完整质量门（Ruff lint、Ruff format check、Pyright、默认 pytest、socket pytest、docs build）。
- [x] 复核 diff/status，按 Trellis 提交门形成独立 `Fix:` 工作提交（`e43511f732de366cadabe8f9674a028ee013a281`）。
- [x] 写入子任务 commit/验收元数据并完成该子任务 Trellis 收尾。

## Gate 2：子任务二（仅 Gate 1 全绿后）

- [x] 启动 `07-21-harden-release-v0-2-0`。
- [x] 实施 reusable CI、release DAG、严格 tag/版本/产物校验、wheel+sdist 冒烟及发布契约测试。
- [x] 新增 `CHANGELOG.md`，更新 `docs/releasing.md`，只修正 README 中相关过时测试/发布说明。
- [x] 运行发布契约定向测试、完整本地质量门、docs build、干净构建和全新 wheel/sdist 安装冒烟。
- [x] 复核 diff/status，形成发布加固提交 `61f099f5f81a06c001bf7b433bf45a85d724fe69`、权限链修复 `3c3995635b878310af3474e7eb7f592985708673` 与文档修正 `b4ada030712ac1055626a0bddfe14fa769111851`，并补齐 Trellis 元数据。

## Gate 3：release candidate

- [x] push `main`/目标提交（只在提交和工作区复核后）。
- [x] 等待目标 SHA 的 GitHub CI 成功。
- [x] 再次查询本地 tag、远端 tag、GitHub release、PyPI/TestPyPI，确认 `v0.2.0` 未占用。
- [x] 报告最终 commit SHA、全部验证结果、tag `v0.2.0`、发布 DAG、剩余风险。
- [x] 只请求一次最终发布确认；未确认则停止。

## Gate 4：不可逆发布（仅最终确认后）

- [x] `git tag -a v0.2.0 -m "v0.2.0" <final-sha>`。
- [x] 最后复核 tag 指向并 `git push origin v0.2.0`。
- [x] 持续观察对应 GitHub Actions run，直到成功或明确失败。
- [x] 验证 PyPI `bareagent-cli==0.2.0` 页面和文件。
- [x] 在全新隔离环境从正式 PyPI 安装，执行 `import bareagent` 与 `bareagent --help`。
- [x] 汇报发布 URL、workflow URL/结论、安装验证结果。

## Gate 5：Trellis 完成

- [x] 更新子任务二和父任务的验收项、commit/tag/release 元数据。
- [x] 评估并完成必要 spec 更新。
- [x] 按 Trellis 流程归档任务并写 session journal，确认最终状态/历史干净且完整。
