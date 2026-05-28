# LSP 客户端集成

## Goal

为 BareAgent 增加 Language Server Protocol (LSP) 客户端，让智能体能调用 pyright / typescript-language-server / rust-analyzer 等成熟工具拿到**精确的符号导航 + 类型诊断**，而不是依赖 grep / 启发式正则。MVP 落地 4 个工具（`lsp_outline` / `lsp_definition` / `lsp_references` / `lsp_diagnostics`），覆盖 BareAgent 三大主流使用场景：Python 自身开发、TypeScript 项目、Rust 项目。

行业参考：Serena (24.7k★) 和 Cline (62k★) 都把 LSP 作为核心能力，提供"看代码结构 → 跳定义 → 找引用 → 写改动后立刻知道有没有引入新错误"的闭环。

## Requirements

### 协议层
- 用 `multilspy` (PyPI; Microsoft, 578★, 2026-04 活跃) 处理 Content-Length framing + initialize/initialized/shutdown/exit lifecycle + capability negotiation + 12 个 server 的下载/启动适配。**不自写 transport / framing**
- multilspy 放 `[lsp]` optional extra（避免 base install 拉 tree-sitter 等依赖）：`uv pip install -e ".[lsp]"`
- 不装 `[lsp]` extra 时 BareAgent 启动 LSP 模块 graceful skip + 在 `/lsp status` 提示 "extra not installed"

### LanguageServerManager
- 自写 `src/lsp/manager.py::LanguageServerManager`：管理多个 `multilspy.SyncLanguageServer` 实例（multilspy 是单实例单语言），按文件扩展名路由到正确 server
- 并发启动（参考 Serena `ls_manager.py` 思路；只抄架构不抄代码）：所有声明的语言 server 并行 `initialize`，单个失败标 UNHEALTHY 并跳过，不阻塞 REPL boot
- 启动超时（默认 15s — pyright 启动 + 全项目索引 5-15s，rust-analyzer 可能更慢），超时标 UNHEALTHY
- 进程崩溃 → on_disconnect callback（复用 MCP PR6 模式 + BackgroundManager.notify 通道）→ 标 UNHEALTHY + console 推送
- `/lsp reload <language>` 重启指定语言 server
- shutdown → exit 两阶段 lifecycle 编排（区别于 MCP 的 close pipe 模式）

### 工具（4 个 Tier 1，按 LSP method 价值密度排序）
按 `lsp_<verb>` 前缀注入到 `DEFERRED_TOOLS`：

| 工具名 | LSP method | 用途 |
|---|---|---|
| `lsp_outline(file)` | `textDocument/documentSymbol` | 文件符号树（class / function / method / variable，含 line range）— LLM 看新文件第一步，比 read_file 全文便宜 10x |
| `lsp_definition(file, line, col)` | `textDocument/definition` | 跳定义，替代 grep，跨 re-export / import 准 |
| `lsp_references(file, line, col)` | `textDocument/references` | 找引用，重构/影响面分析 |
| `lsp_diagnostics(file)` | `textDocument/diagnostic` (pull, LSP 3.17+) → fallback `publishDiagnostics` cache | 写后验证，闭环修复；带 race condition 处理（参考 Serena `analysis_complete` Event 模式） |

**坐标系约定（必须明确）**：
- 工具暴露给 LLM 是 **1-based** (line, col)，匹配编辑器习惯
- handler 内部转换为 **0-based** 调 LSP（LSP 规范是 0-based）
- LSP 返回的 0-based 位置 → handler 转 1-based 给 LLM
- 在工具 schema description 里明文写

### subagent 集成
- `AgentType` 加 `lsp_tools_enabled: bool = True`（平行于 PR4 引入的 `mcp_tools_enabled`）
- `explore` / `plan` / `code-review` 三种 read-only 类型默认 `True`（LSP 这 4 个工具是只读）
- 未来写类操作（v2 的 rename）会按需切换
- `filter_tools()` / `filter_handlers()` 按 `lsp_tools_enabled=False` 剥掉 `lsp_*` 工具

### Hybrid auto-diagnostics-on-edit（默认 OFF）
- `src/core/handlers/edit_file.py` + `write_file.py`：成功后 try-getNewDiagnostics hook
- diff 算法（参考 Cline `getNewDiagnostics`）：edit 前 snapshot diagnostics → edit 后 snapshot → 取**新增**的（不显示已存在的）拼成纯文本附在 tool result 末尾：
  ```
  Newly introduced diagnostics in src/foo.py:
  - [pyright Error] Line 12: Cannot assign to variable 'x' because of its type
  ```
- **默认 OFF**：`[lsp] auto_diagnostics_on_edit = false`
- LSP 未启 / config OFF → hook noop（零开销，handler 不感知）
- 实现位置：`src/lsp/diagnostics.py::diagnostics_diff_after_edit(lsp_manager, file_path)`；handler 调用时 LSP 未装时返 `None`

### REPL 命令
- `/lsp status` — 列各 language server 状态（starting / running / unhealthy / stopped）+ 启用的工具数 + capability
- `/lsp list` — 列当前可用 `lsp_*` 工具
- `/lsp reload <language>` — 重启指定语言 server（kill + 重新 initialize）

实现参考 PR4 的 `/mcp` 命令；空格前缀（区别于 `/mcp:` 的 prompt 触发）。

### 配置
```toml
[lsp]
auto_diagnostics_on_edit = false   # Hybrid hook 默认 OFF
start_timeout = 15.0               # 单 server 启动 + initial analysis 超时

[[lsp.servers]]
language = "python"                # multilspy code_language
extensions = [".py", ".pyi"]       # 文件扩展名 → 这个 server 的路由
# initialization_options 透传给 LSP server
# initialization_options = { python = { pythonPath = "/path/to/.venv/bin/python" } }

[[lsp.servers]]
language = "typescript"
extensions = [".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"]

[[lsp.servers]]
language = "rust"
extensions = [".rs"]
```

### 命名空间
所有 LSP 工具以 `lsp_<verb>` 前缀（单下划线，区别于 MCP 的 `mcp__<server>__<tool>` 双下划线）。

### 权限
- LSP 工具走现有 `PermissionGuard`：DEFAULT 模式 4 个工具都是只读、自动通过；AUTO/PLAN 同此
- 危险模式检测不适用（LSP 输入是 JSON 不是 shell 文本）
- v2 加写类操作（rename）时需要更细致的 permission 设计

## Acceptance Criteria

1. `uv pip install -e ".[lsp]"` 拉 multilspy；不装 extra 时 BareAgent 启动 LSP 模块 graceful skip + `/lsp status` 提示 "extra not installed"
2. `[[lsp.servers]]` 配置加载，并发启动 Python + TS + Rust 三个 server，handshake 成功，工具按 `lsp_*` 注入 `get_tools()`
3. LLM 调 `lsp_outline("src/main.py")` → 返回文件符号树（pyright 解析）
4. LLM 调 `lsp_definition("src/main.py", 42, 10)` → 跳到符号定义（坐标 1-based）
5. LLM 调 `lsp_references("src/main.py", 42, 10)` → 列引用
6. LLM 调 `lsp_diagnostics("src/main.py")` → 返回 pyright 诊断；优先 pull 失败回退 push cache
7. `edit_file` 引入新错误 + `auto_diagnostics_on_edit=true` → tool result 末尾追加 `Newly introduced diagnostics...` 段
8. `auto_diagnostics_on_edit=false` → tool result 不含 diagnostics 段（默认 OFF）
9. explore / plan / code-review 子代理可以调 `lsp_*` 工具（默认 `lsp_tools_enabled=True`）
10. 设置 `lsp_tools_enabled=False` 的 agent_type → 子代理调 `lsp_*` 报 tool not available
11. kill LSP server 子进程 → 控制台立刻 unhealthy 通知 + `/lsp status` 反映
12. `/lsp reload python` 能恢复挂掉的 pyright
13. server 启动超过 15s → 标 unhealthy + 报警 + 不阻塞 REPL boot
14. BareAgent 退出后无 LSP 僵尸子进程（atexit + SIGTERM handler 兜底，参考 MCP PR6）
15. Windows 路径在 LSP URI 转换处不踩 `file:///C:/` vs `file:///C%3A/` 不一致问题
16. 坐标系：工具传 1-based，LSP 调用用 0-based，返回再转回 1-based
17. ≥ 15 个 pytest unit case + 1 个 `_manual.py` E2E（真实 pyright `pip install pyright` 跑通）

## Definition of Done

- 新增 `src/lsp/` 子包；模块布局遵循 `.trellis/spec/backend/directory-structure.md`
- pytest 全绿（不退化现有 461 个 case）；`ruff check src tests` + `ruff format` 全绿
- `tests/test_lsp_*.py` 覆盖关键路径（≥ 15 case）+ `tests/test_lsp_e2e_manual.py` 真实 pyright 端到端
- `CLAUDE.md` 与 `.trellis/spec/backend/directory-structure.md` 增加 `src/lsp/` 模块说明
- `config.toml` 增加完整 `[[lsp.servers]]` 注释示例（Python + TS + Rust 各一）
- `pyproject.toml` 加 `[lsp]` extra（`multilspy>=0.1.x`）

## Out of Scope（v1 不做）

- **写类 LSP 操作**：`rename` / `codeAction` / `formatting` / `applyEdit` server-initiated request 接受 → v2
- **Tier 2/3 工具**：`hover` / `workspace_symbol` / `implementation` / `declaration` / `completion` / `signatureHelp` / `callHierarchy` / `typeHierarchy` → v2
- **抽象出 `src/jsonrpc/` 共享层**：v1 独立 `src/lsp/`，不动 `src/mcp/`；v2 视代码复用价值评估
- **自写 LSP transport + framing**：完全依赖 multilspy（不在源码里复刻 Content-Length reader / lifecycle 编排）
- **Java/C/C++/Go 等更多语言**：v1 只默认带 Python/TS/Rust；用户可自行加 `[[lsp.servers]]`，但 BareAgent 不预置 adapter
- **跟 tree-sitter repo-map（Aider 风格）的整合**：不同 niche
- **Server-initiated request 完整支持**：multilspy 已处理基础几个（`window/showMessage` 等），v1 不扩展自定义 handler
- **多 workspace folder**：v1 默认单 folder（BareAgent cwd）；多 folder → v2
- **LSP 工具结果 payload 上限**：v1 不做截断；error 路径已 string fallback
- **LSP 配置热重载**：编辑 config.toml 不重启 — 跟 MCP 一致留 ROADMAP

## Technical Approach

### 模块布局
```
src/lsp/
├── __init__.py           # 公共导出
├── manager.py            # LanguageServerManager: 多 server 并发管理 + 文件路径→server 路由
├── tools.py              # 4 个工具 schema + handler（调 multilspy SyncLanguageServer）
├── config.py             # [[lsp.servers]] 配置解析 + LSPConfig dataclass
├── diagnostics.py        # diff 算法 + pull/push 双轨 + race condition 处理
├── coord.py              # 1-based ↔ 0-based 转换 + DocumentUri 工具 (Windows 友好)
└── errors.py             # LSPError / LSPHandshakeError / LSPCallError
```

### 关键集成点
1. **`src/main.py::load_config`**：新增 `[lsp]` + `[[lsp.servers]]` 段解析 → `LSPConfig`
2. **`src/main.py::main`**：在 `agent_loop()` 前调 `LanguageServerManager.start_all()`；复用 MCP PR6 的 atexit + SIGTERM 注册路径
3. **`src/core/tools.py::get_tools` / `get_handlers`**：接受 `lsp_manager` 参数，注入 LSP schemas + handlers
4. **`src/planning/agent_types.py`**：`AgentType` 增加 `lsp_tools_enabled: bool = True`；`filter_tools` / `filter_handlers` 应用过滤
5. **`src/core/handlers/edit_file.py` / `write_file.py`**：成功后调 `lsp_manager.diagnostics_diff_after_edit(file)`；LSP 未启/config OFF/无变化都返 `None`，handler 仅在非 None 时追加
6. **`src/main.py` REPL 命令**：新增 `/lsp status` / `/lsp reload` / `/lsp list` 路由（参考 PR4 `/mcp` 命令实现风格）

### multilspy 集成边界
- BareAgent 不直接 import multilspy 的 `MultilspyConfig` 给用户配置；自己定义 `LSPServerConfig` → 转换为 multilspy 配置
- multilspy 单实例单语言 → `LanguageServerManager` 内部维护 `dict[language, SyncLanguageServer]`
- 文件路径 → 语言：用 config 声明的 `extensions` 做后缀匹配（lowercased + `pathlib.PurePath`）
- multilspy 内部已处理 Content-Length + lifecycle + capability + Windows URI；BareAgent 信任它（不重新封装）
- `import multilspy` 在 `src/lsp/__init__.py` 顶层做 try-except，未装 extra 时所有 `lsp_*` 操作 graceful no-op

### 拆 PR 建议

MVP 估算 ~1500-1800 LOC（不含 multilspy 自身代码量）。建议拆 **2 个 child task**：

- **child A: src/lsp/ 骨架 + 工具**
  - src/lsp/{__init__,manager,config,tools,coord,errors}.py
  - 4 个工具 handler + schema 注册到 DEFERRED_TOOLS
  - AgentType.lsp_tools_enabled + filter_tools 集成
  - 单元测试 ≥ 12 case
  - 不带 hybrid hook / REPL 命令 / E2E
  - 验收：单独跑 `pytest tests/test_lsp_*.py` 全绿，但 BareAgent main 路径不会自动起 LSP（feature flag 隐藏）

- **child B: 集成 + UX + E2E + 文档**
  - src/lsp/diagnostics.py + edit/write handler hook（hybrid auto-diagnostics）
  - src/main.py atexit + SIGTERM + REPL `/lsp status|list|reload` 路由
  - tests/test_lsp_e2e_manual.py 真实 pyright 跑通
  - CLAUDE.md + directory-structure.md + config.toml + pyproject.toml `[lsp]` extra
  - 验收：父任务全 17 项 AC 闭环

如父 PRD 通过，brainstorm 收尾时一并把 child A / child B 任务建出来。

## Decision (ADR-lite)

**Context**: BareAgent 当前所有"理解代码结构"的能力依赖 grep + 启发式 + LLM 自己的语言模型知识。这在复杂代码（dynamic import / 跨 re-export / 类型推断）下容易出错。LSP 是工业级精度的解决方案，跟 MCP / 子智能体系统设计哲学一致（让能力可插拔）。

**Decision**: 落地 LSP MVP，技术栈选 multilspy + 自写 LanguageServerManager wrapper + Hybrid 暴露策略。覆盖 Python/TS/Rust 三主流语言、4 个核心工具、subagent 集成、REPL 命令、auto-diagnostics（默认 OFF）。

**Consequences**:
- ✅ 模型在复杂导航 / 重构场景下精度显著提升（pyright 跨 re-export 准确性 vs grep）
- ✅ Hybrid hook 提供"写完代码立刻知道错没错"的闭环（Cline / Serena 的核心 UX）
- ✅ subagent + permission + agent_types 集成走现有抽象（不引入新概念）
- ⚠️ 引入可选依赖 multilspy（拉 tree-sitter-languages + per-server adapter，只在 `[lsp]` extra 安装）
- ⚠️ pyright + tsserver + rust-analyzer 同时启动占 RSS ~600MB–1.5GB；MVP 不做内存限制，可在配置关闭部分语言
- ⚠️ multilspy 单实例单语言，多语言路由全靠 BareAgent 自写 wrapper（架构风险）

## Technical Notes

- multilspy 项目: <https://github.com/microsoft/multilspy>（看 `src/multilspy/lsp_protocol_handler/server.py` 的 Content-Length 实现 + 12 个 server adapter）
- 行业参考: Serena LanguageServerManager <https://github.com/oraios/serena/blob/main/src/serena/ls_manager.py>（**架构思路抄、代码不抄**）
- LSP 3.17 规范: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification>
- 坐标转换：工具 docstring/schema 明确写 "line and column are 1-based (matching editor convention)"
- Windows URI：multilspy 已处理 `file:///C:/` 形式（不带 `%3A` 编码）；自写代码若涉及构造 URI 走 multilspy `path_utils`
- pyright analysis_complete race condition：implement 期实测 multilspy 是否处理（看 `pyright_server.py`）；如未处理，自己加 `threading.Event` 模拟 Serena 模式
- pyright wrapper 是 npm package（PyPI `pyright` 包是 wrapper，会去 spawn node）；用户本地没 Node 时 pyright wrapper 会自下载 — 在 README / CLAUDE.md 标注
- jdtls / Java 启动慢（60s+），MVP 不带 Java，避免冷启动体验差
- 与 MCP PR6 复用：on_disconnect callback 链路 / atexit + SIGTERM / NotificationManager 推送通道 — 全部走 MCP 现有抽象（如 BackgroundManager.notify）

## Research References

- [`research/lsp-vs-mcp-protocol.md`](research/lsp-vs-mcp-protocol.md) — LSP framing / lifecycle / 与 MCP 协议层差异矩阵 / 4 个 server 启动配置 / Windows URI 坑 / 抽象提层建议
- [`research/lsp-agent-integration-patterns.md`](research/lsp-agent-integration-patterns.md) — multilspy/solidlsp/pylspclient 对比 / Serena+Cline+Continue 暴露模式 / 方法价值密度排序 / Hybrid 模式 + getNewDiagnostics 实现参考
