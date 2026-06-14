# 发布到 PyPI（Releasing）

BareAgent 通过 **git tag 触发 GitHub Actions + PyPI Trusted Publishing (OIDC)** 自动发布，
**不在 GitHub Secrets 里存任何 PyPI token**。版本号由 **hatch-vcs 从 git tag 自动派生**
（打 tag = 定版本，无需手改 `pyproject.toml`）。

工作流文件：[`.github/workflows/release.yml`](../.github/workflows/release.yml)

- **推 `vX.Y.Z` tag** → 构建并发布到正式 **PyPI**
- **手动 `Run workflow`（workflow_dispatch）** → 发布到 **TestPyPI**（演练用）

---

## 一、一次性设置（首次发布前必做）

OIDC Trusted Publishing 需要在 PyPI 端登记一个「pending publisher」绑定，把
「哪个 GitHub 仓库的哪个 workflow」授权为可发布者。**字段必须完全匹配**，任一不符都会
导致发布步骤 403（`invalid-publisher`）。

本项目的绑定值：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `bareagent-cli` |
| Owner | `525300887039` |
| Repository name | `BareAgent` |
| Workflow name | `release.yml` |
| Environment name | `pypi`（PyPI）/ `testpypi`（TestPyPI） |

### 1. PyPI（正式）

1. 登录 <https://pypi.org>，打开 **<https://pypi.org/manage/account/publishing/>**。
2. 在「Add a new pending publisher」选 **GitHub**，按上表填入，Environment name 填 `pypi`。
3. 点 **Add**。

> 项目无需预先存在：首次成功发布时 PyPI 会自动创建 `bareagent-cli` 项目并转正这个 pending publisher。
> 注意：pending publisher **不预留名字**——发布前先确认 `bareagent-cli` 在 <https://pypi.org/project/bareagent-cli/> 仍未被占用。

### 2. TestPyPI（演练，独立账号）

TestPyPI 是与 PyPI **完全独立**的注册表（独立账号、独立绑定）。

1. 在 <https://test.pypi.org> 注册/登录（与 PyPI 不是同一账号）。
2. 打开 **<https://test.pypi.org/manage/account/publishing/>**，同样填上表，但 Environment name 填 `testpypi`。
3. 点 **Add**。

### 3. GitHub Environments

仓库 **Settings → Environments**：

- 新建环境 `pypi`，建议添加 **Required reviewers**（每次正式发布前需人工点 Approve，纵深防御）。
- 新建环境 `testpypi`（演练环境，通常无需审批）。

---

## 二、正式发布一个版本

```bash
# 1. 确保 main 干净、CI 绿
git switch main && git pull

# 2. 打版本 tag（X.Y.Z 语义化版本；hatch-vcs 会据此定版本号）
git tag -a v0.1.0 -m "v0.1.0"

# 3. 推 tag —— 触发 release.yml
git push origin v0.1.0
```

随后 GitHub Actions：构建 sdist+wheel → `twine check` → （若配了 reviewer）等待审批 →
通过 OIDC 发布到 PyPI。发布后任何人可：

```bash
uv tool install bareagent-cli      # 或 pipx install bareagent-cli
bareagent --help
```

> **版本号不可覆盖**：PyPI 不允许重传同一版本/文件，删除也是永久的。发布失败需修复后
> **打一个新的更高版本 tag** 重发（不要复用同号 tag）。

---

## 三、发布前演练（TestPyPI dry run）

正式 tag 之前，可先把整条管线（OIDC 握手、构建、上传）在 TestPyPI 上跑一遍：

1. GitHub 仓库 **Actions → Publish to PyPI → Run workflow**（即 `workflow_dispatch`）。
2. 它会构建当前分支并发布到 TestPyPI。未打 tag 的提交版本形如 `0.1.1.devN`
   （已配置 `local_scheme = "no-local-version"`，去掉 `+local` 段以便可上传；重复演练靠
   `skip-existing` 不报错）。
3. 验证：`uv tool install --index-url https://test.pypi.org/simple/ bareagent-cli`。

---

## 四、原理与排错要点

- **OIDC，无 token**：`id-token: write` 只加在发布 job（非全局），运行时换取 PyPI 短时令牌。
- **`fetch-depth: 0`**：checkout 必须取全历史+tag，否则 hatch-vcs 看不到 tag、版本号错。
- **杂牌 tag**：仓库历史有非版本 tag（如 `backup-before-email-rewrite`）；`pyproject.toml`
  的 `tag-pattern` 已限定只认 `vX.Y.Z`，不受其干扰。
- **403 / invalid-publisher**：逐一核对 owner / repo / workflow 文件名 / environment 四项是否与
  PyPI 绑定完全一致。
- **Linux only**：`pypa/gh-action-pypi-publish` 是 Docker action，只能在 `ubuntu-latest` 上跑。
