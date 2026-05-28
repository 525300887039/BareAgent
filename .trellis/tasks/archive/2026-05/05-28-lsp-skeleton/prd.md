# Child A: src/lsp/ 骨架 + 4 工具 + agent_types 集成

父任务：`.trellis/tasks/05-27-lsp-client/prd.md`

本 PR 落地 LSP MVP 的**核心代码骨架**：multilspy 集成、`LanguageServerManager`、4 个工具 handler、subagent 类型集成。**不** 包含 UX / E2E / 文档（那些归 child B）。

## Goal

让 BareAgent 在 `uv pip install -e ".[lsp]"` 后，通过 config + tool registry 注入 4 个 LSP 工具（`lsp_outline` / `lsp_definition` / `lsp_references` / `lsp_diagnostics`），LLM 能调通 pyright 拿到符号导航 + 诊断。subagent 类型按 `lsp_tools_enabled` 过滤。

本 PR **不上**：atexit、SIGTERM、REPL 命令、hybrid auto-diagnostics hook、真实 pyright E2E、CLAUDE.md / directory-structure.md / config.toml 更新。这些都在 child B。

## Requirements

### 1. pyproject.toml [lsp] extra
- 新增 `[project.optional-dependencies]` 下 `lsp = ["multilspy>=0.1.x"]`（implement 期实测确认 multilspy 当前 PyPI 版本号）
- 不影响 base install；不装 extra 时 LSP 模块 graceful skip

### 2. src/lsp/ 包结构
```
src/lsp/
├── __init__.py     # 公共导出 + multilspy 可选 import (try-except)
├── config.py       # [[lsp.servers]] 解析 + LSPConfig / LSPServerConfig dataclass
├── manager.py      # LanguageServerManager: 多 server 并发管理 + 文件路径路由
├── tools.py        # 4 个工具 schema + handler factory
├── coord.py        # 1-based ↔ 0-based 转换 + DocumentUri 工具
└── errors.py       # LSPError / LSPHandshakeError / LSPCallError
```

### 3. LSPConfig 数据类
```python
@dataclass(slots=True)
class LSPServerConfig:
    language: str                     # multilspy code_language（"python" / "typescript" / "rust"）
    extensions: list[str]             # 文件后缀 → 路由（lowercased，含点）
    initialization_options: dict | None = None

@dataclass(slots=True)
class LSPConfig:
    servers: list[LSPServerConfig]
    auto_diagnostics_on_edit: bool = False   # 本 PR 仅解析、不消费（child B 用）
    start_timeout: float = 15.0
```

`parse_lsp_config(raw: dict) -> LSPConfig`：从 TOML 顶层取 `lsp` 段；不存在返空 config（servers=[]）；重复 language 拒绝。

### 4. LanguageServerManager
```python
class LanguageServerManager:
    def __init__(self, config: LSPConfig, console: UIProtocol | None = None) -> None: ...
    def start_all(self) -> None: ...                                  # 并发启动；失败标 UNHEALTHY，不抛
    def get_server_for_file(self, path: str) -> SyncLanguageServer | None: ...  # 按 extension 路由
    def get_status(self, language: str) -> ServerStatus | None: ...
    def iter_running(self) -> Iterator[tuple[str, SyncLanguageServer]]: ...
    def reload(self, language: str) -> None: ...                       # 重启某语言（child B 会通过 REPL 调）
    def close_all(self) -> None: ...                                   # 同步 shutdown → exit
```

- `ServerStatus` enum：`STARTING / RUNNING / UNHEALTHY / STOPPED`（同 `MCPManager.ServerStatus`）
- 并发启动：`ThreadPoolExecutor`，max_workers = len(servers)
- 启动超时：每个 server `concurrent.futures.Future.result(timeout=config.start_timeout)`
- 单 server 失败 / 超时 → 标 UNHEALTHY + `console.print_error`
- `multilspy.SyncLanguageServer.create(...)` 包装；底层 multilspy 已处理 framing / lifecycle
- multilspy 未安装（`from multilspy import ...` ImportError）→ `start_all()` no-op，所有 server 标 UNHEALTHY 并附 reason "multilspy extra not installed"
- on_disconnect 钩子：本 PR **预留接口**（`set_on_disconnect(cb)` setter），child B 真正接到 console / notifier

### 5. 4 个工具 handler
位于 `src/lsp/tools.py`：

```python
def build_lsp_tools(manager: LanguageServerManager) -> tuple[list[dict], dict[str, Callable]]:
    """Return (schemas, handlers) for the 4 Tier-1 LSP tools."""
```

**坐标系约定**：工具 schema description 必须明确：
> "line and column are 1-based (matching editor convention). Position (1, 1) is the very first character."

工具：
- `lsp_outline(file: str)`
  - 调 `manager.get_server_for_file(file).request_document_symbols(file_uri)`
  - 输出：缩进式符号树文本（class / function / variable，含 line range 1-based）
- `lsp_definition(file: str, line: int, col: int)`
  - 1-based → 0-based 内部转换
  - 调 multilspy `request_definition`
  - 输出：定义位置（`<file>:<line>:<col>` 1-based）+ 周边 1-2 行代码片段
- `lsp_references(file: str, line: int, col: int)`
  - 同上坐标转换
  - 输出：所有 reference 列表（`<file>:<line>:<col>` per row）
- `lsp_diagnostics(file: str)`
  - 优先 pull（multilspy 是否支持 `request_text_document_diagnostics` 由 implement 期实测）；不支持就读 multilspy publishDiagnostics cache
  - 输出：每条诊断 `[severity] Line N: message`

**错误降级**（参考 MCP PR5 沉淀）：
- 无路由的 server（文件后缀无对应 [[lsp.servers]]）→ 返 `Error: no LSP server configured for <extension>`
- server UNHEALTHY → 返 `Error: language server <lang> is unhealthy`
- multilspy 抛异常 → 返 `Error: LSP call failed: <type>: <msg>`
- 文件不存在 → 返 `Error: file not found: <path>`
- 位置越界 → 返 LSP server 的原始 error message

注册到 `DEFERRED_TOOLS`（不进 `BASE_TOOLS`）— 与 mcp__/team_/load_skill 同款延迟加载。

### 6. coord.py — 坐标 + URI
```python
def line_col_1_to_0(line: int, col: int) -> tuple[int, int]: ...        # LLM 1-based → LSP 0-based
def line_col_0_to_1(line: int, col: int) -> tuple[int, int]: ...        # LSP 0-based → LLM 1-based
def path_to_document_uri(path: str) -> str: ...                          # Windows 友好；走 pathlib.PurePath 规范化
def document_uri_to_path(uri: str) -> str: ...                           # 反向
```

URI 实现：优先委托 multilspy `path_utils`（如可用）；否则手写 `file:///<absolute-path>`，Windows 上盘符大写、不做 `%3A` 编码（spec 上 server / multilspy 都能接受这种形式）。

### 7. tool registry 集成
`src/core/tools.py::get_tools` 和 `get_handlers`：
- 增 `lsp_manager: LanguageServerManager | None = None` 参数
- 当 `lsp_manager is not None` 时把 `build_lsp_tools(lsp_manager)` 返回的 schemas + handlers 合并进延迟工具集
- 注册到 `DEFERRED_TOOLS`（与 mcp__/team_/load_skill 同款）

### 8. AgentType.lsp_tools_enabled
`src/planning/agent_types.py`：
- `AgentType` 加 `lsp_tools_enabled: bool = True`（平行 `mcp_tools_enabled`）
- `_READ_ONLY_DEFAULTS`：lsp_tools_enabled = True（read-only 子代理可以用 LSP 只读工具）
- `filter_tools()` / `filter_handlers()`：当 `lsp_tools_enabled=False` 时剥掉 `lsp_*` 前缀的工具

### 9. src/main.py 最小集成
**本 PR 仅做最小接入**（让工具能从 REPL 跑通；UX / lifecycle 由 child B 完整）：
- `load_config`：增 `[lsp]` + `[[lsp.servers]]` 段解析 → `LSPConfig`
- `main()`：在 MCPManager 后构造 `LanguageServerManager(config.lsp, console=ui_console)` + `manager.start_all()`
- `get_tools` / `get_handlers` 调用增 `lsp_manager=lsp_manager`
- **不做**：atexit / SIGTERM / REPL `/lsp` 命令 / hybrid hook（全部 child B）

## Acceptance Criteria

1. `src/lsp/` 包结构按 PRD 落地（6 个文件）
2. `pyproject.toml` 有 `[project.optional-dependencies] lsp = ["multilspy>=..."]`，base install 不拉 multilspy
3. multilspy 未装时 `import src.lsp` 不抛；`LanguageServerManager.start_all()` 把所有 server 标 UNHEALTHY 且不阻塞 main
4. `parse_lsp_config({"lsp": {...}})` 正确产出 `LSPConfig`；重复 language 拒绝；缺失字段拒绝
5. mock 一个 fake `SyncLanguageServer` 注入 manager → `start_all()` 后 `iter_running()` 出现所有 mock server
6. `lsp_outline(file)` → 调 mock `request_document_symbols` → 返回缩进式符号树
7. `lsp_definition(file, line=10, col=5)` → mock 收到 `(line=9, col=4)` 的 0-based 参数（坐标转换正确）
8. `lsp_references(file, line=10, col=5)` → 返回 mock 给的所有 ref 位置（1-based 输出）
9. `lsp_diagnostics(file)` → 优先 pull 失败回退 push cache（mock 两种情况都覆盖）
10. 4 个工具的错误降级路径都返回 `Error: ...` string 而非抛异常
11. `AgentType(lsp_tools_enabled=False)` → `filter_tools()` 剥掉所有 `lsp_*`
12. `AgentType(lsp_tools_enabled=True)` + `mcp_tools_enabled=False` → `lsp_*` 留、`mcp__*` 剥（独立开关）
13. `src/main.py::main` 构造 LanguageServerManager 并通过 `get_tools` 把 4 个工具注入到 tool registry
14. `ruff check src tests` + `ruff format --check src tests` 全绿
15. 新增 ≥ 12 pytest unit case，全绿；现有 461 case 不退化

## Definition of Done

- `pytest -q`：473+ passed / 0 failed
- `ruff check src tests` 0 issue
- 没动 `src/mcp/*` / `src/permission/*` / `src/core/loop.py` / `src/provider/*` / `src/core/handlers/{edit,write}_file.py`
- 不写 CLAUDE.md / directory-structure.md / config.toml / E2E manual / REPL 命令（全部归 child B）

## Out of Scope（child B 做的）

- `src/lsp/diagnostics.py` 模块 + Hybrid auto-diagnostics-on-edit hook
- atexit + SIGTERM handler 兜底 `lsp_manager.close_all`
- REPL `/lsp status` / `/lsp list` / `/lsp reload`
- on_disconnect callback 接到 console + BackgroundManager.notify
- `tests/test_lsp_e2e_manual.py` 真实 pyright 跑通
- `CLAUDE.md` / `.trellis/spec/backend/directory-structure.md` / `config.toml` 文档同步

## Technical Notes

- multilspy 单实例单语言 → manager 内部 `dict[language, SyncLanguageServer]`
- multilspy 的 `SyncLanguageServer` 创建方式：`SyncLanguageServer.create(config, logger, repository_root)`；config 类型由 multilspy 定义 — 实现期看 multilspy README 确定
- multilspy `request_document_symbols` 返回 LSP `DocumentSymbol[]` 或 `SymbolInformation[]`（两种 schema，spec 兼容）— handler 内统一格式化为缩进文本
- multilspy 的 logger 接口需要传入；接到 stdlib `logging.getLogger("src.lsp.multilspy")`
- Mock 测试策略：定义一个轻量级 `FakeSyncLanguageServer`（同 multilspy 公共方法签名），注入 manager 跑测试，避免真实启动 pyright
- 在 `LanguageServerManager.__init__` 内用 `from multilspy import SyncLanguageServer` lazy import + try/except；ImportError 走 graceful path
- 不在本 PR 加 NotificationManager 注入（child B 加）；本 PR 只让 manager 持有 `console` 引用即可

## Research References

继承父任务的：
- `.trellis/tasks/05-27-lsp-client/research/lsp-vs-mcp-protocol.md`
- `.trellis/tasks/05-27-lsp-client/research/lsp-agent-integration-patterns.md`
