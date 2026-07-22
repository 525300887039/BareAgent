# 技术设计：可复用质量门与产物驱动发布

## 基线问题

现有 `.github/workflows/ci.yml` 只响应 main push 和 pull request；release tag 不触发它。现有 release workflow 的 publish jobs 仅 `needs: [build]`，build 只运行 `uv build` 和 `twine check dist/*`，因此存在五个缺口：

1. tag/dispatch 可绕过 Ruff、Pyright、默认 pytest 和 socket pytest；
2. `dist/*`/`dist/` 可能混入旧产物；
3. 没有从构建 wheel/sdist 做隔离安装验证；
4. tag 触发器 `v*` 和 `startsWith` 不是严格 SemVer；
5. actions 使用可移动 tag/branch，发布 action 还是 `release/v1` branch。

## CI 单一来源选择

将纯质量 jobs 提取到 `quality.yml` 并增加 `workflow_call` trigger；`ci.yml` 与 release 都以
job-level reusable workflow 调用它。main-only notifier 保留在 `ci.yml`，避免 release 的
read-only caller 在展开 reusable workflow 时遇到 `issues: write` 权限升级。release 增加：

```yaml
quality:
  uses: ./.github/workflows/quality.yml
```

取舍：

- 优点：PR/main 与 release 真正执行同一份 Ruff、Pyright、Linux+Windows 默认 pytest 和 Linux socket job；现有 CI failure issue notifier 仍由 `ci.yml` 维护。
- notifier 不进入 reusable permission chain；`quality.yml` 只需要 `contents: read`，release 无需授予 `issues: write`。
- reusable workflow 的内部 `test`/`socket` jobs 在 caller 中表现为一个原子 `quality` job；GitHub 只有在所有必需内部 jobs 成功时才把调用判为成功。publish 显式 `needs: quality`，因此不能跨过内部测试。
- 不选复制命令或新 shell wrapper，避免 Windows/条件矩阵和 socket 路径再次漂移。

## Release DAG

```text
quality (reusable quality.yml: test matrix + socket)

build (strict ref/version + clean build + inventory + twine + artifact)
  ├── smoke-wheel (fresh env, wheel install, resources + CLI)
  └── smoke-sdist (fresh env, sdist build/install, resources + CLI)

[quality, build, smoke-wheel, smoke-sdist]
  ├── publish-to-pypi      (tag push only)
  └── publish-to-testpypi  (workflow_dispatch only)
```

quality 与 build 可以并行节省时延，但任何 publish 都显式等待全部四个 caller-visible jobs。smoke jobs 自身 `needs: build`，publish 同时列出 build 是有意的显式契约，而不是只依赖传递关系。

## Tag 与 hatch-vcs 版本验证

新增 stdlib-only `scripts/release_contract.py`，把安全关键规则变成可单测行为：

- 严格 tag regex：`^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$`，使用 full match。
- 只要 ref type 是 tag（包括手动选择 tag 的 dispatch），就严格校验并得到 expected version。
- 对正式 push，必须是严格 tag ref；branch push 不可能进入正式 publish。
- build 后读取 wheel `*.dist-info/METADATA` 和 sdist `PKG-INFO` 的 `Version`，要求二者相等。
- tag 构建要求产物版本与 expected version 完全相等，从构建结果证明 hatch-vcs 派生正确。
- branch dispatch 接受 hatch-vcs 的 dev version，但仍要求 wheel/sdist 元数据一致。

GitHub tag glob 保留 `v*` 只是 trigger 粗筛；严格校验在任何 artifact 上传和 publish 之前失败。workflow expression 不假装提供 regex 能力。

## 干净、确定的构建产物

build 在 GitHub-hosted fresh runner 上仍显式清除 `dist/`，随后 `uv build --out-dir dist`。`release_contract.py` 要求目录恰有一个 `.whl` 和一个 `.tar.gz`，拒绝额外发布文件。后续命令只使用：

```text
dist/*.whl
dist/*.tar.gz
```

twine check、artifact upload 和 publish artifact 均不使用宽泛 `dist/*`/`dist/` 来源。artifact action 配置 `if-no-files-found: error`，publish runner 只下载本次 run 的命名 artifact 到空目录，并显式 `packages-dir: dist/`。

## 安装冒烟

新增 `scripts/smoke_installed_package.py`，由 wheel/sdist jobs 各自在 fresh GitHub runner + fresh venv 中运行。脚本使用 `importlib.resources.files("bareagent")` 验证：

- `import bareagent` 已成功；
- `config.toml` 存在；
- `skills` 目录及当前内置 `code-review`、`git`、`test` 的 `SKILL.md` 存在。

workflow 另执行 venv 中的 `bareagent --help`。安装输入必须是下载 artifact 下的 wheel 或 sdist，命令中禁止 `-e`。脚本从 `scripts/` 启动，src-layout 不会因仓库根目录自动提供 `bareagent`，因此 import 来自隔离环境安装。

## 权限与并发

- workflow 顶层设空权限或最小只读；checkout jobs 仅 `contents: read`。
- quality 调用只传 `contents: read`；CI 的 main-only notifier 不进入 reusable workflow。
- smoke jobs无写 repository 权限。
- 两个 publish jobs各自仅 `id-token: write`；不 checkout，不授予 contents/issues/actions write。
- `concurrency.group` 使用 workflow + ref，`cancel-in-progress: false`，避免同一 ref 重复发布同时运行，也不取消正在 OIDC 上传的 run。

## Immutable actions

把 CI/release 中所有第三方 actions 固定到规划时解析的 40 位 SHA，并在行尾保留版本注释：

- `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09`（v5）
- `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b`（v8.1.0）
- `actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4`（v5）
- `actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53`（v6）
- `pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247`（release/v1 当前 commit）

本地 reusable workflow 引用 `./.github/workflows/quality.yml` 不是第三方依赖，无 SHA pin 要求。

## 发布契约测试

不新增 YAML 解析依赖。沿用 `tests/test_ci_visibility.py` 的 stdlib/raw-text 静态保护风格，新建 `tests/test_release_workflow.py`（或同等单文件）覆盖：

- `ci.yml` 与 release quality job 都调用 reusable `quality.yml`，后者包含五项质量命令且不请求写权限；
- 两个 publish job 的 `needs` 包含 `quality`、`build`、`smoke-wheel`、`smoke-sdist`；
- strict tag validator 接受标准版本并拒绝 `v*` 非 SemVer变体；
- wheel smoke 安装 `dist/*.whl`，且 workflow command lines 中无 editable `-e`；
- clean dist、exact globs、artifact name 和 inventory validator 不可移除；行为测试向临时 dist 加旧文件并断言拒绝；
- publish job permissions 只有 `id-token: write`，非 publish job 无该权限；
- 所有外部 `uses:` 都是 40 位 SHA。

`pyproject.toml` 的 sdist include 增加 `.github/workflows/quality.yml` 与 `.github/workflows/release.yml`，确保 shipped tests 所依赖的布局文件齐全。

## TestPyPI 与正式发布

- `workflow_dispatch` 只走 TestPyPI，使用当前 ref 的 hatch-vcs version；branch 通常得到不可覆盖的 dev version。
- 取消用 `skip-existing` 把重复版本伪装成新演练成功；重复版本失败时，确认既有 artifact 或新增 commit 获得新 dev version后再演练。
- 正式 tag push 只走 PyPI。环境分别为 `testpypi` / `pypi`，均通过 Trusted Publishing。
- 正式发布前的 release candidate 报告和用户最终确认位于 Git tag 之前，不由 workflow 内部条件代替。

## 文档与版本说明

- `CHANGELOG.md` 按 provider/效率、代码理解、session/agent、multimodal、可靠性/发布等用户可见主题总结，不列 bookkeeping commits。
- `docs/releasing.md` 以实际 DAG 和不可覆盖事实重写相关段落。
- README 只把“全部测试”改为默认 suite，并补充 socket、lint/format、Pyright、docs build 命令；保留既有结构。
