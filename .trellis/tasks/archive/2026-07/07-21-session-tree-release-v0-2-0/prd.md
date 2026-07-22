# 修复 session tree 环形数据并发布 v0.2.0

## 目标

按严格顺序交付并独立验收两项工作：先修复损坏 sidecar 中环形 session tree 的可见性，完成验证并形成独立提交；再加固发布链路、形成独立发布候选提交，并在获得最终发布确认后发布 `v0.2.0`。

## 子任务与顺序

1. `07-21-fix-session-tree-cycles`：修复 session tree 纯环、自环和混合森林渲染。
2. `07-21-harden-release-v0-2-0`：加固 CI/发布/产物安装验证，准备并发布 `v0.2.0`。

第二个子任务必须等待第一个子任务满足全部验收项、完成完整质量门并形成独立 commit 后才能启动。父任务只负责来源需求、顺序门、跨任务验收与最终集成复核，不直接承载代码实现。

## 需求

- 工作基线必须是干净的 `main`，并在每个不可逆或提交步骤前复核本地与 `origin/main`；保留所有用户已有改动，不覆盖、不混入提交。
- 版本只由 hatch-vcs 和 Git tag 派生，不新增或手工维护第二份版本号。
- 两个子任务的代码/测试/文档提交必须逻辑独立；Trellis 归档与 journal 元数据按流程另行记录。
- 不实施 `main.py` 拆分、插件系统、新 provider 或其他无关功能。
- 规划材料经用户审阅并明确确认实施后，才可 `task.py start`；首先只启动第一个子任务。
- 发布候选完成后，必须先报告最终 commit SHA、全部验证、待创建 tag、workflow 发布路径与剩余风险，并且只请求一次“最终发布确认”。此前不得创建或 push `v0.2.0` tag，也不得触发正式 PyPI 发布。
- 若任一时点发现本地、远端或 PyPI 已存在 `v0.2.0`，停止发布路径并立即报告，不覆盖版本或 tag。
- 发布成功后持续验证 GitHub Actions、PyPI 页面以及全新环境中的 `import bareagent` 和 `bareagent --help`。

## 验收标准

- [x] 两个子任务的 `prd.md`、`design.md`、`implement.md` 均经用户审阅后才进入实施。
- [x] session tree 子任务先完成全部功能、回归测试和完整质量门，并形成不含发布工作的独立提交。
- [x] 发布子任务只在前项完成后启动，并形成不含 session tree 实现的独立发布加固提交。
- [x] release workflow 无法绕过 Ruff、Pyright、默认 pytest、socket pytest、构建、twine check 与安装冒烟。
- [x] `CHANGELOG.md`、发布文档和 README 的相关说明与真实自动化链路一致。
- [x] 发布前 `main`/目标提交已 push 且 GitHub CI 成功，本地/远端/PyPI 再次确认不存在 `v0.2.0`。
- [x] 未获最终发布确认前没有正式 tag/PyPI 副作用。
- [x] 获得最终发布确认后创建 annotated `v0.2.0`、push、观察到明确 workflow 结论，并完成 PyPI 全新安装验证。
- [x] Trellis 子任务验收项、commit 元数据、归档信息和 session journal 完整，父任务完成最终集成复核。

## 非目标

- 不顺带重写 README、ROADMAP 或架构。
- 不放宽 Ruff、Pyright、pytest 或 socket 测试规则。
- 不使用手工上传替代自动化链路，也不引入长期 PyPI API token。
