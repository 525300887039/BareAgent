# PyPI 发布合规改造 + tag 触发自动发布 CI

## Goal

把 BareAgent 从"本地可 `pip install -e .`"提升为"可正式发布到 PyPI、并由 git tag 触发 GitHub Actions 自动构建发布"的合规状态。先补齐法律/元数据合规缺口,再接通 CI/CD 自动发布管线。

## What I already know(上一轮合规体检结论)

绿灯(无需改):
* PyPI 包名 `bareagent` 可用(pypi.org 返回 404,未被占用)
* 打包的 `config.toml` 不含真实密钥(只有 `*_env` 引用名 + 注释示例),打进 wheel 安全
* 本地真实密钥在 `config.local.toml`,**未被 git 跟踪**、也**不在** force-include/sdist 清单 → 不会外泄
* `.gitignore` 已忽略 `config.local.toml` / `.logs` / `.transcripts` 等
* 依赖全 permissive:anthropic=MIT、openai=Apache-2.0、httpx/prompt-toolkit=BSD、rich/pypdf/multilspy=MIT/BSD → 可合法 MIT 分发,无 copyleft 传染
* git 远端:`github.com/525300887039/BareAgent`(CI 落点)
* 构建后端 Hatchling + entry point `bareagent = "src.main:main"` 已就绪

红灯(发布前必须修):
* **无 LICENSE 文件**:README 徽章写 MIT 但无 LICENSE,pyproject 也无 `license` 字段(法律硬伤)
* **`src` 包名污染**:`packages = ["src"]` 装进 site-packages 会撞名;连带 `main.py:135` 的 `Path(__file__).parent.parent` 配置定位在改名后失效
* **pyproject 元数据缺失**:无 `readme`(→ PyPI 不显示描述)/`authors`/`license`/`classifiers`/`[project.urls]`/`keywords`

黄灯(体验问题):
* README 全是相对路径图片(`docs/**/*.png`)→ PyPI 页面断图
* 版本号硬编码 `0.1.0`,PyPI 不允许覆盖同版本号 → 需 bump 策略

## Decisions(用户已拍板)

1. **CI 触发方式**:git tag(`v*`)触发自动发布(非 push-to-main 即发)
2. **发布凭证**:PyPI Trusted Publishing (OIDC) —— 不在 GitHub Secrets 存长期 token
3. **版本号来源**:hatch-vcs 从 git tag 自动派生版本(打 tag = 定版本)
4. **License**:MIT

## Requirements(final)

**A. src-layout 重构**
* `src/*` → `src/bareagent/*`;514 处 `from src.` / `import src.` 机械改名为 `bareagent.`(含 `tests/`)
* 入口 `bareagent = "src.main:main"` → `bareagent.main:main`
* `config.toml` + `skills/` 移入 `src/bareagent/` 包内
* 运行时资源定位改 `importlib.resources`(两处):`main.py` 的 `DEFAULT_CONFIG_PATH`、`skills.py:resolve_skills_dir`(用 Traversable `iterdir()`,避开 `as_file` 目录生命周期坑)
* `pyproject.toml` packages 改 `["src/bareagent"]`,删旧 `force-include` 块

**B. 合规元数据 + LICENSE**
* 新增 `LICENSE`(MIT,`Copyright (c) 2026 ducat`)
* pyproject 补 `license`、`readme = "README.md"`、`authors`、`classifiers`、`keywords`、`[project.urls]`(Homepage/Repository/Issues)
* README 发布版图片相对路径 → GitHub raw 绝对 URL(修 PyPI 断图)

**C. hatch-vcs 版本**
* `[build-system] requires` 加 `hatch-vcs`;`version` 进 `dynamic`;`[tool.hatch.version] source = "vcs"`
* 限定 `v*` tag-pattern(规避杂牌 tag `backup-before-email-rewrite`);可选 `version-file` 暴露 `__version__`
* 验证:真 `vX.Y.Z` tag 下 `hatch version` 正确;editable install 仍可用

**D. CI/CD(`.github/workflows/release.yml`)**
* 触发:`push: tags: ['v*']` → build(`actions/checkout` `fetch-depth: 0`)→ 正式 PyPI 发布(`pypa/gh-action-pypi-publish`,`environment: pypi`,publish job `id-token: write`)
* `workflow_dispatch` → 发 TestPyPI(`environment: testpypi` + `repository-url`)dry-run
* 文档:列出 pypi.org + test.pypi.org 上 Trusted Publishing pending-publisher 绑定的手动步骤(owner=`525300887039`,repo=`BareAgent`,workflow=`release.yml`)

## Open Questions

* (无 — 已全部收敛)

## Decision: TestPyPI

* **要。** release workflow 加 `workflow_dispatch` 手动触发 → 发 TestPyPI 的 dry-run 路径(独立 `environment: testpypi` + `repository-url`);tag `v*` 触发 → 正式 PyPI。用户需在 test.pypi.org 上额外做一次 Trusted Publishing 绑定。

## Decision: 署名

* `authors = [{name = "ducat", email = "no.525350@gmail.com"}]`
* LICENSE:`Copyright (c) 2026 ducat`
* git config 已是 `ducat <no.525350@gmail.com>`(global),无需改动

## Decision: src-layout(ADR-lite)

* **Context**:`packages = ["src"]` 致 site-packages 出现 `src/` + 裸 config/skills,撞名风险;首个 PyPI 版本永久留底
* **Decision**:**选 A —— 本任务即做 src-layout**(`src/*` → `src/bareagent/*`)
* **量化**:514 处 `from src.` import、145 文件,**无字符串/动态/`importlib`/`sys.modules` 引用 src** → 纯机械替换 + `tests/` 全量回归兜底,大但低隐藏风险
* **Consequences**:首发即干净包名;config.toml/skills 移入包内走 `importlib.resources`;入口改 `bareagent.main:main`;diff 大但一次到位,避免日后破坏性改名

## Acceptance Criteria(final)

* [ ] `pytest`(default marker 集)全绿 —— src-layout 改名无回归
* [ ] `bareagent` 命令本地可启动;`import bareagent` 成功(无残留 `import src`)
* [ ] wheel 内导入包为 `bareagent/`,**无** 顶层 `src/` / 裸 `config.toml` / 裸 `skills/`;config.toml + skills 在 `bareagent/` 包内
* [ ] 运行时能定位 config.toml + skills(editable install 与 wheel 安装两种形态都能)
* [ ] `uv build` 产出 wheel + sdist,`twine check dist/*` 通过
* [ ] wheel/sdist 内**不含** `config.local.toml` 或任何真实密钥
* [ ] `LICENSE`(MIT)存在且与 pyproject `license` 字段一致
* [ ] hatch-vcs:真 `vX.Y.Z` tag 下 `hatch version` 输出该版本(不被杂牌 tag 干扰)
* [ ] `ruff check src tests` 通过(只 format 改动文件,不全树 format)
* [ ] release workflow 存在:`v*` tag → PyPI、`workflow_dispatch` → TestPyPI,checkout `fetch-depth: 0`
* [ ] README 在 PyPI 渲染正常(图片不断)
* [ ] 文档列出 PyPI/TestPyPI Trusted Publishing 手动绑定步骤

## Technical Approach

分三段递进(同一 task 内,按依赖顺序):

1. **packaging 重构(基础)**:src-layout 目录移动 + 514 import 机械改名(脚本化 `from src.`→`from bareagent.`、`import src.`→`import bareagent.`)+ 入口改名 + 两处 importlib.resources 资源定位 + pyproject packages → 跑 `pytest` 回归 + `bareagent` 冒烟
2. **合规元数据 + 版本**:LICENSE + pyproject 全元数据 + hatch-vcs(`v*` tag-pattern)+ 删 force-include + README 图片绝对化 → `uv build` + `twine check` + `hatch version`(打临时 `v0.1.0` tag 验证)
3. **CI/CD**:`.github/workflows/release.yml`(tag→PyPI / dispatch→TestPyPI)+ Trusted Publishing 手动绑定文档(README 或 `docs/releasing.md`)

## Implementation Plan(small steps)

* Step 1: src-layout 移动 + import 批量改名 + 入口 + 资源定位 + 修 tests → pytest 绿
* Step 2: LICENSE + pyproject 元数据 + hatch-vcs + 删 force-include + README 图片 → build/twine/hatch version 验证
* Step 3: release.yml(PyPI tag + TestPyPI dispatch)+ 发布绑定文档

## Definition of Done

* 新增/更新测试(打包产物校验、resource 定位若改 src-layout)
* ruff check 通过
* 文档更新(README 安装方式 + 发布流程说明)
* 发布流程含可回滚考量(版本号不可覆盖,失败重发需 bump)

## Out of Scope(待确认)

* 自动 changelog 生成 / release notes 自动化
* 多平台 standalone 可执行文件(PyInstaller)
* Docker 镜像发布
* conda 分发

## Research References

* [`research/trusted-publishing-ci.md`](research/trusted-publishing-ci.md) — tag 触发 OIDC Trusted Publishing 的完整 workflow YAML + pypi/test.pypi 绑定手动步骤 + 坑
* [`research/hatch-packaging.md`](research/hatch-packaging.md) — hatch-vcs git-tag 版本 + src-layout + importlib.resources 的可粘贴 pyproject 片段与运行时模式

## Technical Notes

* 关键文件:`pyproject.toml`、`src/main.py:135`(`DEFAULT_CONFIG_PATH`)、`src/planning/skills.py:32`(`resolve_skills_dir`,存 Path 后 `.glob()` → 改 Traversable `iterdir()`)、`README.md`、新增 `LICENSE` + `.github/workflows/release.yml`
* **杂牌 tag `backup-before-email-rewrite`**:`git describe` 会误抓 → hatch-vcs 需限定 `v*` tag-pattern(setuptools_scm `tag_regex`/`git_describe_command`)并用真 `vX.Y.Z` tag 验证 `hatch version`
* Trusted Publishing:`permissions: id-token: write`(仅 publish job)+ `pypa/gh-action-pypi-publish@release/v1` + `environment: pypi`;owner=`525300887039` repo=`BareAgent` workflow=`release.yml`
* hatch-vcs:`requires = ["hatchling","hatch-vcs"]` + `[tool.hatch.version] source = "vcs"` + `dynamic = ["version"]`;可选 `[tool.hatch.build.hooks.vcs] version-file` 暴露 `__version__`
* CI checkout 必须 `fetch-depth: 0`
* config.toml/skills 移入 `src/bareagent/` 包内 → 删除旧 `force-include` 块(hatchling 自动打包包内非 .py 文件)
* PyPI 名 `bareagent` 已确认 404 可用(发布前不被占即可抢注,pending publisher 不预留名)
