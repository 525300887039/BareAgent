# 语义重命名工具 semantic_rename（基于 LSP textDocument/rename）

## Goal

给 LLM 一个**引用感知的安全重命名**工具：调用 `semantic_rename(file, line, col, new_name)` →
经 LSP `textDocument/rename` 让语言服务器算出一个跨文件的精确编辑集合（WorkspaceEdit）→
落盘。区别于现有 `edit_file` + `grep` 的纯文本替换（会误伤同名符号/字符串/注释、跨文件漏改），
这是符号级、不该碰的不碰、跨文件自动跟进的重命名。

## What I already know（代码尽调结论）

- **LSP 层已就绪**：`src/lsp/`（manager / tools / coord / diagnostics）已落地，4 个只读工具
  `lsp_outline/definition/references/diagnostics` 在用。
- **multilspy 0.0.15 没有 `request_rename` 同步包装**：`SyncLanguageServer` 只有
  definition/references/completions/document_symbols/hover/workspace_symbol 6 个同步方法。
  → 跟 pull-diagnostics 一样，rename 必须走「裸 LSP 请求」。
- **裸请求通路已验证**：内层 `server.send.rename(params)`（`LspRequest.rename` → 发
  `textDocument/rename`）**存在**，`prepare_rename` 也在。async→sync 桥接复用 multilspy 自己的
  模式：`asyncio.run_coroutine_threadsafe(coro, sync_server.loop).result(timeout=...)`，
  并用 `language_server.open_file(relpath)` 先 didOpen（与 `request_definition` 内部一致）。
- **写盘**：`src/core/fileutil.py:atomic_write_text` 复用；坐标 1↔0 转换用 `src/lsp/coord.py`。
- **权限**：新工具名不入 `PermissionGuard.SAFE_TOOLS` → DEFAULT 必确认 / AUTO 通过 / PLAN 拒绝，
  正是写工具应有行为；`format_preview` 已能渲染 JSON 参数，无需改 guard 主体。
- **工具注册**：schema 进 `LSP_TOOL_SCHEMAS`-邻近位置、handler 经 `build_lsp_tools(manager)`
  注入 `core/tools.py`；DEFERRED_TOOLS 机制现成。
- **子代理隔离坑**：read-only 子代理（explore/plan/code-review）`lsp_tools_enabled=True`，
  若沿用 `lsp_` 前缀，写工具会被它们错误保留 → 必须解决（见 Technical Approach）。

## Requirements（evolving）

- 新增 client tool `semantic_rename(file, line, col, new_name)`，坐标 1-based（对齐现有 LSP 工具）。
- 经语言服务器 `textDocument/rename` 取 WorkspaceEdit，解析 `changes` 与 `documentChanges`
  两种形态，跨文件应用 TextEdit（单文件内按位置倒序应用避免位移）。
- 落盘走 `atomic_write_text`；返回「改了哪些文件 + 每个文件几处」的摘要。
- 走正常写权限：DEFAULT 确认 / AUTO 通过 / PLAN 拒绝；read-only 子代理拿不到该工具。
- LSP 不可用 / 无服务器路由 / 返回空编辑 → 显式 Error（不静默退化）。
- 单元测试覆盖：WorkspaceEdit 解析（两种形态）、倒序应用、跨文件、错误路径、权限/隔离；
  jedi 真实 E2E（manual 标记，对齐现有 `test_lsp_e2e_manual.py`）。

## Acceptance Criteria（evolving）

- [ ] `semantic_rename` schema + handler 注入，1-based 坐标转 0-based 调 LSP。
- [ ] WorkspaceEdit `changes` 形态正确应用。
- [ ] WorkspaceEdit `documentChanges`（TextDocumentEdit）形态正确应用。
- [ ] 单文件多处编辑按 (line,char) 倒序应用，结果与逐条独立应用一致。
- [ ] 跨文件重命名：定义文件 + 引用文件全部更新。
- [ ] LSP 不可用 / 无路由 / 空编辑 → 明确 Error 文案，不改任何文件。
- [ ] DEFAULT 模式触发确认；PLAN 模式拒绝；read-only 子代理工具列表中无此工具。
- [ ] 资源型操作（CreateFile/RenameFile/DeleteFile）出现时安全跳过 + 提示（MVP 不做文件级重命名）。
- [ ] ruff / pytest / pyright 全绿；新增行为有测试。

## Definition of Done

- 单元测试 + jedi manual E2E 覆盖；ruff/pytest/pyright 绿。
- CLAUDE.md「LSP 客户端」段同步新增工具；config 无新增项（沿用现有 LSP 配置）。
- 子代理隔离与权限语义有测试佐证。

## Technical Approach（待你确认要点见下）

1. **桥接位置**：在 `LanguageServerManager` 加 `request_rename(abs_path, line0, col0, new_name)`
   方法，封装 `run_coroutine_threadsafe` + `open_file` + `server.send.rename`，把 multilspy
   内部耦合关在 manager.py（与 diagnostics monkey-patch 同处），返回原始 WorkspaceEdit | None。
2. **WorkspaceEdit 应用**：新建 `src/lsp/workspace_edit.py`（解析 changes/documentChanges →
   按 uri 分组 → 单文件内倒序应用 TextEdit → atomic_write_text），纯函数好测。
3. **工具命名 + 隔离**：命名 `semantic_rename`（**不带 `lsp_` 前缀**——它是写工具，不是只读 LSP
   查询），并加入 `_READ_ONLY_DEFAULTS["disallowed_tools"]`。这样 `lsp_*`=只读查询、
   `semantic_rename`=写，读写边界干净，且不被 `lsp_tools_enabled=True` 误放行。

## Decision (ADR-lite)

**Context**: multilspy 无现成 rename 同步包装但裸请求可用；该工具会写盘，权限/子代理隔离/
回退语义需明确边界。

**Decisions（已与用户确认，全部按推荐）**:
- D1 — **无 grep+正则回退**。LSP 不可用 / 无路由 / 空编辑 → 显式 Error，让调用方自行决定是否退
  `edit_file`。理由：静默退化成纯文本替换会摧毁「安全重命名」承诺，把精确与尽力而为混在一个工具
  里调用方无法区分。
- D2 — **不发 prepareRename 预校验**。多一次往返；非法位置已被 D1 空编辑分支兜住。
- D3 — **无 dry-run 预览**。靠权限确认 + 结果摘要（「改了 N 文件共 M 处」）。
- D4 — **命名 `semantic_rename`（不带 `lsp_` 前缀）** + 加入 read-only 子代理 `disallowed_tools`。
  读写边界：`lsp_*`=只读查询、`semantic_rename`=写；规避 `lsp_tools_enabled=True` 误放行写工具。

**Consequences**: 工具语义纯粹（要么精确成功要么明确失败）；回退/预览/文件级重命名留作后续扩展位
（ROADMAP 3.2 smartRelocate）。

## Out of Scope（explicit）

- grep+正则回退（除非 Q1 改判）。
- 文件级重命名 / 移动（workspace/willRenameFiles、smartRelocate）——ROADMAP 3.2 后续项。
- dry-run 预览模式、prepareRename 预校验（除非改判）。
- 重命名后自动重跑 diagnostics（auto-diagnostics 钩子是 edit_file/write_file 专属，本工具不接）。

## Technical Notes

- 关键文件：`src/lsp/tools.py`、`src/lsp/manager.py`、`src/lsp/coord.py`、
  `src/core/tools.py`、`src/planning/agent_types.py`、`src/core/fileutil.py`、
  `src/permission/guard.py`、`tests/test_lsp_tools.py`、`tests/test_lsp_e2e_manual.py`。
- multilspy 内部：`SyncLanguageServer.loop`（事件循环线程）、`.language_server`（async）、
  `.language_server.server.send.rename(params)`、`.language_server.open_file(relpath)`。
- params 形态：`{textDocument:{uri}, position:{line,character}, newName}`，uri 用
  `coord.path_to_document_uri`。
