# Child B: LSP 集成 + UX + E2E + 文档

父任务：`.trellis/tasks/05-27-lsp-client/prd.md`
前置：`.trellis/tasks/archive/2026-05/05-28-lsp-skeleton/` (已 archive，commit `3b427aa`)

本 PR 把 child A 落地的骨架"激活成可用产品"：hybrid auto-diagnostics、生命周期硬化、REPL 命令、真实 pyright E2E、文档同步。完成后父任务 17 项 AC 全部闭环、可一并 archive。

## Goal

把 child A 的 src/lsp/ 骨架从"工具能调通 mock server"推进到"用户 `pip install pyright && bareagent` 后立刻能 `lsp_outline` / `lsp_diagnostics` 跑真实 pyright 拿到结果"，外加 Hybrid auto-diagnostics（edit/write 后追加新增诊断 diff，默认 OFF）。

## Requirements

### 1. src/lsp/diagnostics.py — diff 算法 + race condition
- 新建 `src/lsp/diagnostics.py`，提供：
  - `snapshot_diagnostics(manager, file_path) -> list[Diagnostic]`：取当前文件诊断（pull → fallback push cache）；返回 normalized dataclass list
  - `diagnostics_diff_after_edit(manager, file_path, before: list[Diagnostic]) -> list[Diagnostic]`：edit 后 snapshot，与 before 做 diff，返**新增**部分（不显示已存在）
  - `format_diagnostics(diags: list[Diagnostic]) -> str`：拼成纯文本，格式：
    ```
    Newly introduced diagnostics in src/foo.py:
    - [pyright Error] Line 12:5 — Cannot assign to variable 'x' because of its type
    ```
- **diff 等价定义**：`(file, line, col, severity, message)` 五元组相同 = 同一条诊断
- **race condition 处理**：child A trellis-check 标记 multilspy 0.0.15 不暴露 pull diagnostics → 走 push cache。本 PR 期间实测 multilspy 的 `language_server.diagnostics` / `_diagnostics` 真实结构（替代 child A 的 best-effort 探测），如确认结构则 inline 直接读，并加 `time.sleep(small)` 或 `threading.Event` 等待 pyright 异步分析完成（参考 Serena `analysis_complete` 模式；exact 实现以实测为准）。
- **顺手清 child A 遗留**：`src/lsp/tools.py` L220-222 的 `pull_error` dead code → 删除或改 `_log.debug(pull_error)`；`_read_push_diagnostics` 真实 multilspy cache 路径以本 PR 实测为准（child A 是 best-effort 探测）。

### 2. Hybrid auto-diagnostics hook
- 动 `src/core/handlers/edit_file.py` + `write_file.py`：
  - 调用 handler 入口处 + 出口处加 hook 调用（参考代码占位实现，handler 主体不变）：
    ```python
    before = lsp_diagnostics_snapshot_if_enabled(...)
    # ...handler 原 body...
    appendix = lsp_diagnostics_diff_if_enabled(..., before)
    if appendix:
        result += "\n\n" + appendix
    ```
  - 提取 helper `_maybe_diagnostics_appendix(lsp_manager, lsp_config, file_path, before)` 在 `src/lsp/diagnostics.py`；LSP 未启 / config OFF / no route / before 取不到 → 返 `None`
- handler 通过 `**ctx` / 闭包拿 `lsp_manager` 和 `lsp_config`（参考 MCP handler 注入路径）
- **默认 OFF**：`config.lsp.auto_diagnostics_on_edit = False`；config gate 在 helper 第一行
- 性能：LSP 未启时 helper 立刻返回（< 1µs），不污染 happy path

### 3. atexit + SIGTERM 兜底
- `src/main.py::main` 在 `LanguageServerManager` 构造后立即：
  ```python
  atexit.register(lsp_manager.close_all)
  # SIGTERM handler 已由 MCP PR6 注册；多注册 LSP 清理时复用同一 lambda（或单独注册）
  ```
- 与 MCP PR6 atexit/SIGTERM 共存：lambda 内既调 mcp_manager.close_all 又调 lsp_manager.close_all（一次 sys.exit(130)）；或分别 atexit.register 两次（Python atexit 按 LIFO 跑，顺序无关）
- LSP `close_all` 走 multilspy 的 shutdown → exit 两阶段（child A 已实现）

### 4. on_disconnect 接通
- `LanguageServerManager` 构造参数增 `notifier: NotificationManager | None`
- `set_on_disconnect` 注册回调：lambda reason → 加锁标 UNHEALTHY + `console.print_error(...)` + `notifier.notify(task_id=f"lsp:{language}", message=...)` 推送
- 与 MCP PR6 `_on_disconnect` 同款模式（复用 BackgroundManager.notify 通道）
- multilspy 内部如何感知 server 进程崩溃：以本 PR 实测为准（multilspy 应该有类似 `on_exit` callback；如无，需要 poll subprocess returncode 自己做）

### 5. REPL `/lsp` 命令
- `src/main.py` 路由：`/lsp status` / `/lsp list` / `/lsp reload <language>`（空格前缀，参考 MCP PR4 `/mcp` 命令风格）
- `LanguageServerManager.summarize()` 新方法：返 `[{"language": ..., "status": ..., "tool_count": 4 if running else 0, "extensions": [...]}]`（参考 MCP `summarize()` 形态）
- `/lsp list` 列当前可用 `lsp_*` 工具及对应 server 状态
- `/lsp reload python` → `manager.reload("python")`（child A 已有方法，本 PR 接通 REPL 路由 + 错误 fallback string）

### 6. tests/test_lsp_e2e_manual.py
- 真实 pyright 跑通（`pip install pyright` 是前提；测试用 `shutil.which("pyright-langserver")` 或 `subprocess.run(["pyright", "--version"])` 检测，缺失时 `pytest.skip`）
- 用例：
  - `test_pyright_handshake`：LanguageServerManager + 单 server config (python) → start_all → iter_running 出现 python server + `MULTILSPY_AVAILABLE=True`
  - `test_pyright_outline`：用 `src/main.py` 跑 `lsp_outline("src/main.py")` → 返回符号树（含 `main` function 等已知符号）
  - `test_pyright_diagnostics`：在 `tmp_path` 写一段故意 type-error 的 Python（如 `x: int = "string"`）→ `lsp_diagnostics(path)` 返非空诊断
  - `test_pyright_definition`：写一段 import / use 的 Python → `lsp_definition` 跳到定义
- `_manual.py` 后缀（项目现有约定，CI 默认排除）
- 不强制 typescript-language-server / rust-analyzer 测试（v1 仅 pyright；其余 server 留用户自行验证，DoD 不要求）

### 7. 文档同步
- `CLAUDE.md` 在 `## 架构` 章节新增小节：**LSP 客户端 (`src/lsp/`)**
  - 简述：LanguageServerManager + multilspy 集成 + 4 个 Tier 1 工具 + Hybrid auto-diagnostics 默认 OFF + subagent 隔离（lsp_tools_enabled）+ REPL `/lsp` 命令
  - 关键文件：`src/lsp/{manager,tools,config,diagnostics,coord}.py`
  - 配置：指向 `config.toml [lsp]` + `[[lsp.servers]]`
  - 依赖：`uv pip install -e ".[lsp]"`（multilspy + tree-sitter-languages）
- `.trellis/spec/backend/directory-structure.md`：
  - top-level layout 加 `src/lsp/` 条目
  - decision tree 加：**新 LSP 集成相关？** → `src/lsp/`，按 manager / tools / transport-handled-by-multilspy / hybrid hook 路由
- `config.toml`：
  - 加注释示例 `[lsp]` + `[[lsp.servers]]`（Python + TS + Rust 三段）
  - 列默认值（`auto_diagnostics_on_edit = false` / `start_timeout = 15.0`）

### 8. NotificationManager 注入清理
- `src/main.py::main` 把 `bg_manager`（NotificationManager / BackgroundManager）也传给 `LanguageServerManager(notifier=bg_manager)`，与 MCP PR6 一致

## Acceptance Criteria

1. `src/lsp/diagnostics.py` 新建，含 `snapshot_diagnostics` + `diagnostics_diff_after_edit` + `format_diagnostics`；diff 等价按 `(file, line, col, severity, message)` 五元组
2. multilspy `language_server.diagnostics` / `_diagnostics` 真实结构经实测确认，`_read_push_diagnostics` 直接读（不是 best-effort 探测）
3. child A 遗留 `pull_error` dead code 清掉；`_read_push_diagnostics` 改为实测路径
4. `auto_diagnostics_on_edit=true` + edit_file 引入新 type-error → tool result 末尾追加 `Newly introduced diagnostics in <file>:` 段
5. `auto_diagnostics_on_edit=false`（默认）→ tool result 不含 diagnostics 段
6. LSP 未启 / no route / multilspy 未装 → hook noop，handler tool result 不变
7. `src/main.py::main` 注册 `atexit.register(lsp_manager.close_all)` + SIGTERM handler 兜底（与 MCP PR6 共存）
8. BareAgent 退出（正常 / SIGTERM）后无 LSP 子进程残留（`ps` / `tasklist` 验证）
9. kill LSP server 子进程 → console 出现 `MCP-style "LSP server <lang> disconnected: <reason>"` 通知；`/lsp status` 反映 unhealthy；下次 LLM 调用看不到该 server 的 `lsp_*` 工具（manager.iter_running 已过滤 + 工具 handler 返 Error string）
10. `/lsp status` 列各 server 状态 + tool_count + extensions
11. `/lsp list` 列当前可用 `lsp_*` 工具
12. `/lsp reload python` 能恢复挂掉的 pyright
13. `tests/test_lsp_e2e_manual.py` 在本地 pyright 环境跑通 4 个 case（无 pyright 时 skip 不 fail）
14. `CLAUDE.md` + `.trellis/spec/backend/directory-structure.md` + `config.toml` 文档同步
15. 父任务 17 项 AC 全部闭环（本 PR 闭合 #7, #8, #11-15, #17 — 共 7 项）
16. 所有现有 pytest 全绿（PR1-PR6 + LSP child A 累计 508 个 case 不退化）；`ruff check src tests` + `ruff format --check src tests` 全绿
17. 新增测试 ≥ 8 个 unit case：diff 算法五元组等价 / format_diagnostics 格式 / hook config OFF / hook LSP 未启 / hook happy path（with mock manager）/ atexit 注册 / on_disconnect 触发 + 推送 / summarize 反映 UNHEALTHY / REPL `/lsp` 路由 / pull_error dead code 清理验证

## Definition of Done

- 父任务 05-27-lsp-client 17 项 AC 全部勾选
- `Feat：` 前缀 commit；commit message 总结 7 项硬化点
- 文档同步完成
- 真实 pyright E2E 验过（本地手跑）
- 沉淀 spec（如适用）：本 PR 的 patterns（如 diff 算法五元组等价）如有可复用价值，写入 `.trellis/spec/backend/`；不强求

## Out of Scope

- **新增 Tier 2/3 工具**（hover / workspace_symbol / rename / implementation / declaration / completion / signatureHelp / codeAction / formatting / callHierarchy / typeHierarchy）→ 留 v2
- **更多语言 server 默认带**（Java / Go / C++）→ 用户可自行加 `[[lsp.servers]]`
- **抽 src/jsonrpc/ 共享层** → v2
- **typescript-language-server / rust-analyzer E2E 测试**（本 PR 只测 pyright；其余靠用户手验）
- **LSP 工具结果 payload 上限截断**（child A 没做、本 PR 不补；用户超大文件再说）
- **多 workspace folder**（单 folder 即可）
- **LSP 配置热重载**

## Technical Approach

### diff 算法
```python
@dataclass(frozen=True)
class DiagnosticKey:
    file: str
    line: int      # 1-based
    col: int       # 1-based
    severity: str  # "Error" / "Warning" / "Info" / "Hint"
    message: str

def diff_diagnostics(before: list[Diagnostic], after: list[Diagnostic]) -> list[Diagnostic]:
    before_keys = {DiagnosticKey.from_diag(d) for d in before}
    return [d for d in after if DiagnosticKey.from_diag(d) not in before_keys]
```

### Hook 接入边界
- 不让 edit/write handler 自己 import `src.lsp`（避免反向依赖）
- 在 `src/lsp/diagnostics.py` 提供 `maybe_diagnostics_appendix(lsp_manager, lsp_config, file_path, before) -> str | None`；handler 通过 ctx / 闭包注入这个 callable
- handler 调用前后 if-else 检查（**不**用 contextmanager — 想错误路径也保证不挂）

### atexit 与 MCP 共存
两个 `atexit.register(...)` 调用即可，LIFO 顺序无所谓；SIGTERM handler 既有的 lambda 内可以串行调两个 close_all。

### multilspy 实测优先级
本 PR 第一件事是 implement 期实测：
- `multilspy.SyncLanguageServer` 的 `diagnostics` 属性结构（dict[uri, list[Diagnostic]] 还是别的？）
- multilspy 是否有 server 进程退出的 callback / event
- multilspy `start_server()` contextmanager 的退出顺序（atexit 触发时是否正确 cleanup）

实测结果直接写进代码 + 注释，**不**再走 best-effort 探测。

## Decision (ADR-lite)

**Context**：child A 已落骨架但 Hybrid hook / atexit / REPL / 真实 pyright E2E / 文档全未做；child A 还遗留两个小尾巴（pull_error dead code + cache 路径 best-effort 探测）。如果不收尾就只是"有工具能调通 mock"，离用户实际可用差最后 30%。

**Decision**：一个 PR 完成 7 项收尾，按主题（diagnostics / lifecycle / UX / E2E / docs）打包，避免拆得太碎。

**Consequences**：
- ✅ 父任务 05-27-lsp-client 闭环可 archive
- ✅ 用户能 `pip install pyright` 后立刻拿到端到端体验
- ✅ Hybrid hook 默认 OFF 不破坏现有 edit/write 体验
- ⚠️ PR diff 较大但每项有 AC 对应

## Technical Notes

- multilspy 0.0.15 不暴露 pull diagnostics 已由 child A trellis-check 确认；本 PR 走 push cache + race condition 等待
- pyright 第一次跑全项目分析可能 10-15s；E2E 测试给宽松 timeout (30s)
- `lsp_manager.close_all` 必须幂等（atexit 可能与 normal cleanup 重复触发，参考 MCP `close_all` 加锁 clear）
- SIGTERM handler 现已注册（MCP PR6），本 PR 在同一 handler 内追加调 lsp_manager.close_all；或单独 atexit.register（推荐后者，幂等且不与 MCP 耦合）

## Research References

继承父任务的：
- `.trellis/tasks/05-27-lsp-client/research/lsp-vs-mcp-protocol.md`
- `.trellis/tasks/05-27-lsp-client/research/lsp-agent-integration-patterns.md`（含 Cline getNewDiagnostics 算法参考 + Serena analysis_complete race 处理）
