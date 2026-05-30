# T3 CI 工作流

## 目标

新增 `.github/workflows/ci.yml`，在 `push` 与 `pull_request` 触发 lint + 测试。现仅有 `deploy-docs.yml`，无测试 CI。

## 依赖

T1：`pytest` 的 `addopts = "-m 'not manual'"` 已默认排除不稳定/手动测试，CI 直接跑 `pytest` 即可。

## 改动

新建 `.github/workflows/ci.yml`：

- 触发：`push`、`pull_request`。
- 步骤：checkout → 安装 uv + Python 3.12 → `uv pip install -e ".[dev]"` → `ruff check src tests` → `pytest`。
- 用 `astral-sh/setup-uv` 安装 uv；`uv venv` 建环境后用 `uv run` 或激活 venv 跑命令（CI 是 Linux 容器，无 Windows 的 stderr exit-1 坑）。

## 验收

- 用 Python 解析 YAML 确认合法：
  `.venv\Scripts\python.exe -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml',encoding='utf-8')); print('ok')"`
  （若无 pyyaml，退化为结构/缩进自检）。
- Actions 本地跑不了，保证语法正确即可。
