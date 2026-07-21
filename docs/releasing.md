# 发布到 PyPI（Releasing）

BareAgent 使用 **Git tag + GitHub Actions + PyPI Trusted Publishing (OIDC)** 发布。
版本由 **hatch-vcs** 从 Git 历史和 tag 派生；不要在 `pyproject.toml` 或包源码中维护第二份版本号，
也不要在 GitHub Secrets 中保存长期 PyPI token。

工作流：

- [`.github/workflows/quality.yml`](../.github/workflows/quality.yml)：PR、main 和 release 共用的纯质量门。
- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)：PR/main 调度与 main 状态通知。
- [`.github/workflows/release.yml`](../.github/workflows/release.yml)：构建、安装冒烟和 OIDC 发布。

## 一、PR 与 main CI

`ci.yml` 直接响应 main push 和 pull request；它与 release workflow 都通过 `workflow_call`
调用只读的 `quality.yml`。质量门包含：

- Ubuntu + Windows 的默认 pytest；
- Linux 上的 Ruff lint 与 Ruff format check；
- Linux 上的 Pyright；
- Linux 上的 localhost socket suite。

main push 失败时，`ci.yml` 内独立的 `notify` job 会维护 `ci-failure` issue。它不在可复用的
`quality.yml` 中，因此 release 的 read-only caller 不会继承或请求 issue 写权限。

本地完整质量门：

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
uv run pytest
uv run pytest -m socket
```

文档是单独的门：

```bash
cd docs
npm run docs:build
```

## 二、发布 workflow 的阻塞路径

tag push 和手动 TestPyPI 演练执行同一条 DAG：

```text
quality (调用完整 quality.yml)

build (严格 ref -> 清空 dist -> wheel+sdist -> 版本/twine 校验 -> artifact)
  ├─ smoke-wheel (全新 venv，从 wheel 安装)
  └─ smoke-sdist (全新 venv，从 sdist 构建并安装)

quality + build + smoke-wheel + smoke-sdist
  ├─ publish-to-pypi      (严格正式 tag push)
  └─ publish-to-testpypi  (workflow_dispatch)
```

两个 publish jobs 都显式 `needs` 四个前置 jobs。只有实际 publish jobs 有
`id-token: write`；测试、构建和冒烟 jobs 没有 OIDC 发布权限。相同 ref 的 runs 串行，且不会
取消已经进入发布过程的 run。

build job 会先删除并重建 `dist/`，且只接受一个 wheel 和一个 sdist。它读取 wheel
`METADATA` 与 sdist `PKG-INFO`：二者版本必须一致；tag 构建还必须与 tag 去掉 `v` 后完全一致。
artifact 和 twine check 只接收本次的 `dist/*.whl` 与 `dist/*.tar.gz`。

wheel/sdist 冒烟都会验证：

- `import bareagent`；
- `bareagent --help`；
- 安装包内的 `config.toml`；
- 内置 `code-review`、`git`、`test` skills 的 `SKILL.md`。

## 三、一次性 Trusted Publisher 设置

PyPI 与 TestPyPI 是两个独立注册表，需要分别配置：

| 字段 | PyPI | TestPyPI |
|---|---|---|
| Project | `bareagent-cli` | `bareagent-cli` |
| Owner | `525300887039` | `525300887039` |
| Repository | `BareAgent` | `BareAgent` |
| Workflow | `release.yml` | `release.yml` |
| Environment | `pypi` | `testpypi` |

对于已经存在的项目，在 PyPI/TestPyPI 项目的 **Manage → Publishing** 页面添加 GitHub
Trusted Publisher；尚未创建的项目则分别在
[PyPI account publishing](https://pypi.org/manage/account/publishing/) 和
[TestPyPI account publishing](https://test.pypi.org/manage/account/publishing/) 添加 pending
publisher。两边的绑定相互独立，owner、repository、workflow 与 environment 必须和上表完全一致。

在 GitHub Settings → Environments 创建 `pypi` 与 `testpypi`。正式 `pypi` 环境建议配置
Required reviewers；等待 reviewer 时 workflow 是 pending，不代表失败或成功。

## 四、TestPyPI 演练

正式 tag 前先把 release candidate commit push 到 main，并等待该 SHA 的 main CI 成功。然后：

1. GitHub Actions → **Publish to PyPI** → **Run workflow**。
2. 观察 quality、build、两个 smoke 和 `publish-to-testpypi` 全部成功。
3. 从 build job 输出取得实际 hatch-vcs dev version，例如 `0.1.1.dev70`。
4. 在全新环境安装该精确版本。TestPyPI 通常没有依赖包，因此只让 BareAgent 本身来自
   TestPyPI，依赖回退到正式 PyPI：

```bash
uv venv .testpypi-smoke
uv pip install --python .testpypi-smoke/bin/python \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  bareagent-cli==<actual-dev-version>
.testpypi-smoke/bin/python -c "import bareagent; print(bareagent.__file__)"
.testpypi-smoke/bin/bareagent --help
```

TestPyPI 演练不使用 `skip-existing`。注册表不允许覆盖同版本文件：如果同一 commit 的 dev
version 已存在，重跑上传应明确失败，而不是伪装成本次 artifact 已发布。确认既有文件正确，或产生
一个经过完整检查的新 commit（从而得到新的 dev version）再演练。

## 五、正式 v0.2.0 / PyPI 发布

发布候选必须满足：工作区干净、完整本地质量门与 docs build 通过、干净 wheel/sdist 本地安装
冒烟通过、目标 commit 已 push、GitHub CI 与 TestPyPI 演练成功，并确认本地/远端/PyPI 均没有
`v0.2.0`。

本地候选构建也使用与 workflow 相同的确定产物约束：

```bash
uv build --out-dir dist --clear --no-create-gitignore
uv run python scripts/release_contract.py \
  --event-name workflow_dispatch --ref-type branch --ref-name main --dist-dir dist
uvx twine check dist/*.whl dist/*.tar.gz
```

得到最终发布确认后，在已报告的最终 commit 上创建 annotated tag：

```bash
git tag -a v0.2.0 -m "v0.2.0" <final-commit-sha>
git push origin v0.2.0
```

GitHub 的 `v*` 只是触发粗筛；workflow 会拒绝任何不是严格
`vMAJOR.MINOR.PATCH` 的 tag，包括预发布后缀、build metadata、多余段和带前导零的数字。

tag push 后持续观察对应 **Publish to PyPI** run，直到 `publish-to-pypi` 成功或出现明确失败。
成功后验证：

```bash
uv venv .pypi-smoke
uv pip install --python .pypi-smoke/bin/python bareagent-cli==0.2.0
.pypi-smoke/bin/python -c "import bareagent; print(bareagent.__file__)"
.pypi-smoke/bin/bareagent --help
```

## 六、失败与重试

- **tag 前失败**：修复后产生新 commit，重跑完整本地门、main CI 和 TestPyPI；不要降低门或手工上传。
- **TestPyPI 版本冲突**：不能覆盖。核对已存在文件，或以新 commit 的新 dev version 重跑。
- **等待 environment approval**：审批后继续同一 run；不要另开并行发布。
- **tag workflow 在 PyPI 上传前失败**：不要移动或重建 tag。修复不涉及 tag 内容的瞬时环境问题时，
  可 rerun 同一个 GitHub Actions run；代码/产物问题需要后续新版本修复。
- **PyPI 已出现部分或完整 `0.2.0` 文件后失败**：不要删除、覆盖、移动 tag 或重传同名文件。
  先核对注册表实际状态；若需要修复，使用更高的新版本（例如 `v0.2.1`）并重新走全部链路。
- **自动化失败但手动上传可行**：仍视为发布链路失败。修复 workflow 和契约测试，不用手工成功替代。

CI 与 PyPI release 链路中的第三方 GitHub Actions 固定到 40 位 commit SHA。升级 action 时先核对
上游 release/安全公告，更新 SHA 与行尾版本注释，并让发布契约测试保持通过。
