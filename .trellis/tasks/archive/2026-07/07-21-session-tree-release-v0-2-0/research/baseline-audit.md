# 基线审计（2026-07-21，Asia/Shanghai）

## Git 与工作区

- 当前分支：`main`。
- 基线 HEAD：`d321dfd270462b1dceb1038782925c0c95b079c8`。
- 执行 `git fetch --prune --tags origin` 后，`HEAD...origin/main` 为 `0 0`。
- 创建 Trellis 任务前工作区无已显示脏文件；其后仅新增本次三个规划态任务目录。
- 本地历史 tag：`v0.1.0`，另有非版本 tag `backup-before-email-rewrite`。
- `v0.1.0..HEAD`：66 个提交。

## 远端与版本占用

- `git ls-remote` 未发现远端 `refs/tags/v0.2.0`。
- GitHub release 列表未显示 `v0.2.0`。
- PyPI 项目名为 `bareagent-cli`；正式 PyPI 最新版和唯一正式版本均为 `0.1.0`。
- `https://pypi.org/pypi/bareagent-cli/0.2.0/json` 返回 404。
- `https://test.pypi.org/pypi/bareagent-cli/0.2.0/json` 返回 404。
- 当前目标 `v0.2.0` 无冲突，但 release candidate 阶段必须再次检查。

## GitHub CI

- 当前 HEAD 的 CI run `29341639544` 成功：
  `https://github.com/525300887039/BareAgent/actions/runs/29341639544`
- 现有 release workflow 最近一次正式 tag `v0.1.0` 发布 run 成功，但它只有 build + twine check，未硬依赖完整 CI 或安装冒烟。

## 版本适配判断

`v0.1.0` 后包含多项向后兼容的用户可见能力：provider cache/Gemini、语义代码搜索与 repo map、session fork/tree、多模态图片与 PDF、可靠性与权限修复。未发现要求升级到 `1.0.0` 的稳定性承诺，也未识别必须以 breaking release 表达的公开不兼容变更。按语义版本，新增能力对应 minor bump，因此 `v0.2.0` 合理。

## 规划阶段工具事实

- 项目工作流要求的活动版 `trellis-brainstorm` skill 未安装；仅 `.trellis/.backup-*` 中有旧副本。本次未把备份当作现行规则，而是按 `.trellis/workflow.md` 的 Phase 1.1 内嵌指引完成规划。
- `agent-reach` CLI 本机不在 PATH；依据该 skill 的 GitHub 路由改用已认证 `gh` CLI，并用 PyPI JSON API做只读检查。
