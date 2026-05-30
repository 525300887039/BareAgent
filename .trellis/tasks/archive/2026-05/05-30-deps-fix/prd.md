# T2 依赖修正

## 目标

修正 `pyproject.toml` 依赖声明的缺漏与重复。本任务独立于其它子任务。

## 改动

1. **补声明 httpx**：在 `[project].dependencies` 加 `"httpx>=0.27",`。
   理由：MCP HTTP transport 直接 `import httpx` 但未声明（隐式依赖 anthropic/openai 传递引入，脆弱）。
2. **删除整个 `[dependency-groups]` 段**：与 `[project.optional-dependencies].dev` 重复。
   保留后者，文档安装命令 `uv pip install -e ".[dev]"` 走它。

## 勿动

- 不动 `config.local.toml`。

## 验收

- `.venv\Scripts\python.exe -c "import tomllib,httpx; tomllib.load(open('pyproject.toml','rb')); print(httpx.__version__)"` 正常打印版本。
- MCP transport 测试全绿（这两个文件含 socket 用例，用 `-m ""` 显式纳入再核）：
  `.venv\Scripts\python.exe -m pytest tests/test_mcp_transport_http_legacy.py tests/test_mcp_transport_http_streamable.py -m ""`
  注：localhost socket 用例若因环境失败属已知非回归。
