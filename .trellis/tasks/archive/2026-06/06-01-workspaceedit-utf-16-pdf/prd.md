# 代码审查修复：WorkspaceEdit 双形态 / UTF-16 偏移 / PDF 页范围越界

## Goal

修复本 session 四个功能提交的代码审查（4 个并行 reviewer）发现的 3 个真问题：2 个 semantic_rename
的 Med（`workspace_edit.py`）+ 1 个多模态读取的 Med（`file_read.py`）。均为正确性问题，修法明确。

## What I already know（审查结论 = 需求来源）

源于对 `bf700ed`(semantic_rename) 与 `291a12b`(多模态) 的独立审查。无开放决策，修法已定。

## Requirements（3 处修复）

### Fix #1 [Med] — WorkspaceEdit `changes`+`documentChanges` 双形态重复应用
- 文件：`src/lsp/workspace_edit.py:_iter_edit_groups`。
- 现状：把 `documentChanges` 与 `changes` 都 merge 进同一 groups dict → 若 server 对同一 URI 两者都给
  （LSP spec 允许 changes 作向后兼容回退），同一处编辑被应用两次 → 倒序 splice 下文件损坏。
- 修法：按 LSP spec 推荐客户端行为——**当存在 `documentChanges` 时只解析它、完全忽略 `changes`**；
  否则才解析 `changes`。（不是两者都解析再 merge。）
- 保留资源操作（含 kind 项）跳过逻辑不变。

### Fix #2 [Med] — UTF-16 code unit vs Python code point 偏移换算
- 文件：`src/lsp/workspace_edit.py`（`_offset_for_position` / `_build_line_starts` 一带）。
- 现状：LSP `character` 是 UTF-16 code unit，代码当成 Python str code point 索引。同一行符号前有
  emoji/非 BMP 字符时偏移按每个 astral 字符错位 1 → 静默改错范围损坏文件。无注释/测试承认。
- 修法：把「UTF-16 单位的 character」正确换算为 Python str 索引——按行扫描，`ord(ch) > 0xFFFF`
  的字符计 2 个 UTF-16 单位（占 1 个 Python 索引），据此把 character（UTF-16 偏移）映射到行内
  Python 列索引，再加行起始偏移。**加清晰注释**说明 LSP 用 UTF-16、multilspy 0.0.15 不协商
  positionEncoding 故默认 UTF-16。
- 注：现有 `lsp_definition`/`lsp_references`（coord.py）也是直接透传 character，但本次仅修
  workspace_edit 的写盘路径（写盘错位会损坏文件，危害远大于只读查询的坐标显示）；coord 的只读
  坐标不在本任务范围（可在注释里点到）。

### Fix #3 [Med] — PDF 页范围越界静默 clamp（与单页不一致）
- 文件：`src/core/handlers/file_read.py:_parse_page_range`。
- 现状：3 页 PDF，`pages="5"` 正确报错「page 5 out of range」，但 `pages="5-5"`/`"4-6"`（start 越界）
  **静默返回第 3 页**——要的页不存在却拿到别的页且无提示。
- 修法：range 的 **start 越界（start > total）时也返回明确 Error**，对齐单页分支文案；或拒绝空选择
  （start_idx >= end_idx）。现有 `start > end` 守卫不覆盖此情形。

## Acceptance Criteria

- [ ] Fix #1：有 documentChanges 时忽略 changes；同一 URI 两形态都给时不重复应用（新测试佐证）。
- [ ] Fix #1：仅 changes（无 documentChanges）时仍正确解析应用（回归）。
- [ ] Fix #2：含非 BMP 字符（emoji）的行，符号前有 astral 字符时重命名范围正确（新测试佐证）。
- [ ] Fix #2：纯 ASCII / BMP 行行为不变（回归）；代码有 UTF-16 换算注释。
- [ ] Fix #3：range start 越界（如 3 页 PDF 的 "5-5"/"4-6"）返回明确 Error，不静默返回别的页。
- [ ] Fix #3：合法 range（"1-5" 端点 clamp 到末页）行为不变（回归）。
- [ ] 全量 ruff / pytest / pyright 全绿；每个 fix 有针对性测试。

## Definition of Done

- 三处修复 + 针对性单测；ruff·pytest·pyright 绿。无新依赖。

## Out of Scope（explicit）

- coord.py 的 `lsp_definition`/`lsp_references` 只读坐标 UTF-16 换算（只读显示，危害小，另议）。
- 审查里的 Low/Nit（no-op edit 计数、didOpen 超时泄漏、非白名单图片 UnicodeDecodeError 友好文案、
  子代理 token 不计入 /cost、PostToolUse is_error 恒 False、hook 子进程 cwd）——本任务不动。
- 任何功能性增强；仅修正确性。

## Technical Notes

- 关键文件：`src/lsp/workspace_edit.py`、`src/core/handlers/file_read.py`、
  `tests/test_lsp_workspace_edit.py`、`tests/test_file_read_multimodal.py`。
- Fix #2 换算函数应纯函数化便于单测；emoji 测试用例如 "😀x = 1" 在 x 前放 astral 字符验证偏移。
- 三处互不耦合，可独立实现与测试。
