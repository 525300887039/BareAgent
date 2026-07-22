# 发布链路基线审计

## 项目版本与打包

- 分发名：`bareagent-cli`；import/console command：`bareagent`。
- `pyproject.toml` 使用 `dynamic = ["version"]`、hatchling + hatch-vcs。
- `tag-pattern` 接受版本 tag，`local_scheme = "no-local-version"` 使未 tag commit 得到 TestPyPI 可接受的 dev version。
- wheel target 为 `src/bareagent`；`config.toml` 与 `skills/**` 位于包内。
- sdist 显式 include `src`、`tests`、`scripts`、`ci.yml`、pre-push hook、pyproject、README、LICENSE，但当前不含 `release.yml`。

## 现有 CI

- triggers：main push、pull request；没有 `workflow_call`，tag 发布不会触发。
- `test`：Ubuntu + Windows 默认 pytest；Ruff lint/format 和 Pyright 只在 Linux matrix leg。
- `socket`：Ubuntu 单独运行 `uv run pytest -m socket`。
- `notify`：只在 main push 运行，依赖 test + socket 并维护 `ci-failure` issue。
- `tests/test_ci_visibility.py` 以 raw-text/stdlb 静态断言保护 uv pytest、socket、Windows、format、Pyright、notify 和 sdist layout。发布契约测试应沿用该风格，避免新增 YAML 依赖。

## 现有 release

- triggers：`v*` tag push 或 workflow dispatch。
- `build`：full checkout、setup uv、`uv build`、`uvx twine check dist/*`、upload `dist/`。
- publish-to-pypi/testpypi 都只 `needs: [build]`，没有 CI 或安装 smoke。
- 正式 job 条件只用 `startsWith(github.ref, 'refs/tags/v')`，不严格。
- 两个 publish jobs 有 OIDC `id-token: write`，非 publish job 没有；这个正确性质要保留并测试。
- TestPyPI 当前 `skip-existing: true` 会让同版本重复 dispatch 表面成功，但不能证明本次 artifact 已上传。
- 没有 concurrency。

## 已知质量基线

用户提供的近期本地基线约为默认 `1398 passed / 47 deselected`、socket `11 passed`；实施时必须记录实际数值而不是把估计写成结果。

## README / releasing 文档差异

- `docs/releasing.md` 仍以 `v0.1.0` 示例和“build -> twine -> publish”描述发布，没有完整质量门、smoke 或明确的失败/重试 DAG。
- README 开发段把 `uv run pytest` 写成“全部测试”，但 socket suite 实际单独选择；命令块缺 format check、Pyright、socket 和 docs build。
- README 已正确链接 `docs/releasing.md`，无需大改安装或其他章节。

## v0.2.0 release-note 主题

66 个提交中包含大量 Trellis archive/journal，不应逐条列入 changelog。建议按以下用户可见主题归纳：

- Provider 与效率：GPT-5 cache 计费/anchor、跨 provider cache abstraction、Gemini、流式与重试稳定性。
- 代码理解：grep output modes、语义 code search、跨语言 tree-sitter repo map。
- 会话与 agent：session fork/tree、goal/workflow/team memory 与权限/持久状态加固。
- 多模态：终端图片输入、vision 能力门控、web_fetch 图片和 PDF document blocks。
- 工程质量：Windows CI、socket coverage、Ruff format pin、Pyright standard、打包/配置修复，以及本次发布链路和 cycle rendering 修复。

这些是向后兼容新增与修复，支持 minor bump `0.1.0 -> 0.2.0`。
