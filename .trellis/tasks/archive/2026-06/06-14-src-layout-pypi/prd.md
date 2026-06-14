# 文档同步打包/发布改动（src-layout 路径 + PyPI 安装）

## Goal

上一任务（06-14-pypi-tag-ci）把项目改成 src-layout 并发布到 PyPI（分发名 `bareagent-cli`，已上线 v0.1.0）。本任务把**文档**同步到这一现状：修正过时的 `src/` 源码路径、补上"从 PyPI 安装"。**范围已与用户敲定 = 同步到现状，不做更大内容刷新。**

## Requirements（已确认，机械同步 + 安装段）

**A. 源码路径同步**（src-layout：`src/<pkg>` → `src/bareagent/<pkg>`）
* 15 个 guide 章节：`docs/guide/ch01,03,05,06,07,08,09,10,11,12,13,14,15,16,17`（`*.md`）中所有 `src/<pkg>/...` 路径引用
* `<pkg>` ∈ `core|main|provider|planning|memory|team|ui|mcp|lsp|hooks|concurrency|tracing|debug|permission`（注意 `src/main.py` → `src/bareagent/main.py`）
* `README.md` 的「项目结构」ASCII 树（约 205 行起）：树根 `src/` → `src/bareagent/`（让结构反映真实包目录）
* **防重复前缀**：已是 `src/bareagent/...` 的不得再加（正则只匹配 `src/` 紧跟上述 pkg 名，故 `src/bareagent/core` 天然不命中）

**B. PyPI 安装方式**
* `docs/guide/ch02-quickstart.md` 的「安装」段:在现有源码安装(`uv pip install -e`)之上，**置顶**加"从 PyPI 安装"：`uv tool install bareagent-cli`（或 `pipx install bareagent-cli` / `pip install bareagent-cli`），说明已发布、装后命令为 `bareagent`
* README 已有 PyPI 安装段（上一任务加过），核对一致即可
* （可选）README 顶部徽章区加 PyPI 版本徽章：`![PyPI](https://img.shields.io/pypi/v/bareagent-cli)`

## Out of Scope（明确不做）

* 重写/新增章节内容、补新特性文档、内容润色（仅"同步到现状"）
* 改 `.trellis/spec/**` 里的 `src.` 示例（dev 规格，非发布文档）
* 改 `tests/` docstring、`docs/node_modules/**`（第三方，已 gitignore）
* VitePress `npm run build` 验证（重、需 node 环境，超范围；用 grep 残留校验代替）
* 代码/配置改动（纯文档任务）

## Acceptance Criteria

* [ ] `docs/guide` + `README.md`（排除 node_modules）中**无残留** `src/<pkg>` 旧路径（grep 验证）
* [ ] **无** `src/bareagent/bareagent/`（双重前缀）
* [ ] README 项目结构树根为 `src/bareagent/`
* [ ] ch02 快速开始含"从 PyPI 安装 `bareagent-cli`"
* [ ] 命令名/导入名表述仍是 `bareagent`（只改路径与安装名，不动命令/导入）
* [ ] 无意外改动 `.trellis/spec`、`tests/`、`docs/node_modules`

## Technical Notes

* 安全替换正则（示意）：`s#\bsrc/(core|main|provider|planning|memory|team|ui|mcp|lsp|hooks|concurrency|tracing|debug|permission)\b#src/bareagent/\1#g`（`src/bareagent/<pkg>` 不命中，天然防双重前缀）
* 分发名 `bareagent-cli`、导入名/命令名 `bareagent`（见 CLAUDE.md「打包与发布」节、docs/releasing.md）
* PyPI 已上线：https://pypi.org/project/bareagent-cli/ （v0.1.0）
