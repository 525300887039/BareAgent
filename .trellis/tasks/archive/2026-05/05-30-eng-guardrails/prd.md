# 工程化护栏修复（健康体检收尾）

## 背景

对 BareAgent 做了一次完整健康体检，结论：

- **代码与架构健康**：ruff 基本干净；分层良好（handler 不反向依赖 `src.lsp`）；自定义异常体系完整；无裸 `except:`。
- **主要缺口是工程化护栏**：缺固化的工具链配置、依赖声明有缺漏/重复、无测试 CI、存在未清理的类型错误。

本父任务统筹 4 个子任务修复这些缺口。

## 子任务

| 编号 | 任务 | 目录 | 依赖 |
|------|------|------|------|
| T1 | 工具链配置固化 | `05-30-toolchain-config` | — |
| T2 | 依赖修正 | `05-30-deps-fix` | — |
| T3 | CI 工作流 | `05-30-ci-workflow` | T1（addopts 默认排除 manual）|
| T4 | 类型错误清理 | `05-30-type-cleanup` | T1（`[tool.pyright]` 配置）|

实现顺序：**T1 → T2 → T3 → T4**（T2 独立，可并入任意位置）。

## 环境纪律（重要）

- Python 由 uv 管理，venv 实跑 3.14.4，pyproject 声明 `requires-python>=3.12`。
- **不在 PowerShell 用 `uv run`**（构建提示走 stderr 会被判 exit 1 并取消同批工具调用）。直接用 venv 可执行：
  `.venv\Scripts\python.exe`、`.venv\Scripts\ruff.exe`、`.venv\Scripts\pyright.exe`。
- 一次只发一条 shell 命令，大段输出先 `| Out-File $env:TEMP\x.txt` 再 Grep 读取。
- 每改一处文件后用 `git diff` / `git status --short` 复核确实落盘。
- 本机 localhost socket 测试（web_viewer、mcp transport http）因端口/超时不稳，属环境问题非回归，默认运行应排除。

## 不在范围 / 勿动

- **不动 `config.local.toml`**（含用户错放的真实 key，字段语义需用户自己改并轮换 key）。
- 工作区有一条 `M uv.lock`（上个会话 uv 重建副作用），提交前再决定还原或保留。

## 验收

- `ruff check src tests` 全绿。
- 默认 `pytest` 0 failed，且 web_viewer/`_manual` 被自动排除。
- pyright error 数降到 0（或记录无法消除的残留）。
- CI workflow YAML 合法。
- 提供提交计划供用户确认后再 commit。
