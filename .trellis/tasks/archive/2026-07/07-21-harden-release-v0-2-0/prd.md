# 加固发布链路并发布 v0.2.0

## 目标

让任何由 tag push 触发的正式 PyPI 发布或由 `workflow_dispatch` 触发的 TestPyPI 演练，都无法绕过完整质量门、干净构建、产物元数据校验和隔离安装冒烟；在发布候选经验证、push 且 GitHub CI 成功后，按单独的最终确认门发布 `v0.2.0`。

## 硬前置条件

- 父任务的 session tree 子任务已完成所有验收、完整质量门和独立 commit，并完成 Trellis 收尾。
- 用户已审阅父任务和两个子任务的规划材料并给出实施确认。
- 启动本任务时本地 `main` 与预期远端状态一致，未知用户改动已隔离。
- 任一检查发现本地 tag、远端 tag 或正式 PyPI 已存在 `v0.2.0`，立即停止发布路径并报告。

## 发布质量门

- PR/main CI 与 release workflow 必须复用同一个质量命令来源，避免两份 Ruff/Pyright/pytest 命令漂移。
- tag push 和 `workflow_dispatch` 均必须通过：
  - `uv run ruff check src tests`；
  - `uv run ruff format --check src tests`；
  - `uv run pyright`；
  - `uv run pytest`；
  - `uv run pytest -m socket`。
- 正式 PyPI 和 TestPyPI publish jobs 都必须显式依赖完整质量调用、build、wheel smoke、sdist smoke。
- 任一前置 job 失败、取消或跳过时，publish 不得执行。

## 构建与安装冒烟

- build runner 上先清空 `dist/`，再构建且只允许本次产生一个 wheel 和一个 sdist。
- 只对明确的 `dist/*.whl` 与 `dist/*.tar.gz` 执行 twine check 和 artifact 上传，不能上传整个可能含旧文件的目录。
- 对 dist inventory 做行为校验；出现额外 wheel、sdist 或其他发布文件时失败。
- wheel smoke 在全新隔离环境中从 artifact wheel 安装，不允许 editable install，并验证：
  - `import bareagent`；
  - `bareagent --help`；
  - 包内 `config.toml`；
  - 内置 `skills/*/SKILL.md` 运行时资源。
- 另设独立 sdist smoke，在全新隔离环境从本次 sdist 构建/安装并执行同样验证。
- 本地 release candidate 验证重复执行干净构建、twine check、wheel smoke；成本合理时同时执行 sdist smoke（本任务设计为必做）。

## 发布安全性

- 保留 PyPI/TestPyPI Trusted Publishing/OIDC，不增加用户名、密码或长期 API token。
- workflow 默认无写权限；只有两个实际 publish job 拥有 `id-token: write`，且不授予不必要的 `contents: write` 等权限。
- 同一 ref 的 release runs 串行，且不在发布中途 cancel 已运行任务。
- GitHub `v*` 触发后必须用行为校验强制严格 `^v[0-9]+\.[0-9]+\.[0-9]+$`；任意前后缀、预发布、缺段或多段 tag 均不能发布。
- 对 tag ref，构建产物中由 hatch-vcs 派生的 wheel/sdist 版本必须与 tag 去掉 `v` 后完全一致。
- 不新增或手工维护另一份项目版本号。
- 所有第三方 GitHub Actions 尽可能固定到 40 位 commit SHA，并保留可读版本注释；若有例外必须记录理由和风险。
- 正式版本或 tag 不覆盖、不移动、不重传。

## 版本与文档

- 目标版本为 `v0.2.0`；若实施阶段发现 breaking change 或版本占用证据，暂停并向用户说明，不自行换版本。
- 新增 `CHANGELOG.md`，按用户可见主题总结 `v0.1.0..v0.2.0`，不机械列出 66 个 commit。
- 更新 `docs/releasing.md`，准确描述 PR/main CI、release 前置门、TestPyPI 演练、正式 tag/PyPI、失败和不可覆盖重试策略。
- 只修正 README 中明显过时的测试/发布说明：补全 format check、Pyright、默认 pytest 与 socket suite、docs build 的区别；不重写 README/ROADMAP。
- 保持 hatch-vcs 为唯一版本源。

## 自动化回归

发布契约测试至少防止：

- 正式 publish 只依赖 build、绕过测试；
- release 没有运行完整质量门；
- 非严格 SemVer 的任意 `v*` 能通过；
- 安装冒烟使用 `-e` 或源码，而非构建 wheel；
- 旧 `dist` 产物被 twine check、artifact 或 publish 一并处理；
- publish job 获得 `id-token: write` 之外的不必要写权限；
- 第三方 action 回退到可移动 tag/branch（本次全部可固定者）。

## 发布候选与确认

- 完成发布加固独立 commit 后，运行完整本地质量门、docs build、干净产物和隔离安装冒烟。
- push `main`/目标提交并等待对应 GitHub CI 成功。
- 运行一次 TestPyPI `workflow_dispatch` 演练并验证从 TestPyPI 安装实际 dev version；若 Trusted Publisher 环境未配置或失败，明确报告，不以手工上传替代。
- 再次确认本地/远端/PyPI 不存在 `v0.2.0`。
- 汇报最终 commit SHA、所有验证结果、tag、workflow 路径和已知风险，然后只请求一次最终发布确认。
- 未获该确认不得创建/push tag 或触发正式 PyPI 发布。

## 验收标准

- [x] session tree 子任务的硬前置满足后才启动本任务。
- [x] PR/main 和 release 复用同一 CI workflow/命令来源，五项 Python 质量门均阻塞发布。
- [x] 两个 publish jobs 显式依赖 quality、build、wheel smoke、sdist smoke。
- [x] 干净 dist、确定产物、twine check、hatch-vcs/tag 版本一致性均有自动化校验。
- [x] wheel 和 sdist 在各自全新环境安装，并通过 import、CLI help、config 和 skills 资源验证。
- [x] OIDC、最小 permissions、concurrency、严格 tag 和 immutable action pins 生效。
- [x] 六类指定发布契约回归均有自动化测试并通过。
- [x] `CHANGELOG.md`、`docs/releasing.md`、README 相关段落准确且 docs build 通过。
- [x] 发布加固形成独立清晰 commit，Trellis commit/验收/journal 元数据完整。
- [x] main/目标 commit 已 push，GitHub CI 和 TestPyPI 演练成功，`v0.2.0` 无冲突。
- [x] 最终确认前已提供完整 release candidate 报告，且没有正式 tag/PyPI 副作用。
- [x] 最终确认后 annotated tag、tag push、GitHub workflow、PyPI 页面和全新 PyPI 安装全部验证成功，或明确报告失败点。

## 非目标

- 不增加新 provider、插件系统、业务功能或 `main.py` 拆分。
- 不放宽质量规则，不把 TestPyPI/PyPI 手动上传当作修复。
- 不全面重写 README、ROADMAP、CI 通知系统或打包架构。
