# 对话导入导出：/export markdown+json + /import

## Goal

让用户把当前对话**导出**成便携格式（Markdown 分享/存档 + 自包含 JSON 可跨 workspace/机器搬运），并能从外部文件**导入**一段对话载入成新会话继续——补上现有「仅 workspace 本地 save/resume」缺失的导入导出能力。对齐 Claude Code 的 `/export`（导出人读 Markdown）+ JSONL 会话模型。

## What I already know（探查结论）

- **现状**：`TranscriptManager`（`src/memory/transcript.py`）把会话存 `.transcripts/<session_id>_<时间戳>.jsonl`（workspace 本地，原始 messages）。命令仅 `/sessions`/`/resume`/`/new`/`/clear`，**无 /export /import**。
- **`/resume` 机制**（main.py:2287，`/import` 直接镜像）：`messages[:] = restored` → `token_tracker.reset()` → 分配 session id（`_set_compact_session_id` + `_set_interaction_logger_session`）→ `_switch_session_mailbox` → `spawned_agents={}` → `_build_handlers(...)` rebuild → `_replay_stdio_transcript(messages, ui_console)` 重放。
- **渲染蓝本**：`_replay_stdio_transcript`（main.py:1803）已遍历 user(str/tool_result) / assistant(str/text/tool_use) / 跳过 system——markdown 导出可复用同样的遍历结构。
- **session id 生成**：`_generate_session_id(transcript_mgr)`（main.py:1057）。
- **messages 形态**：user=str | list[dict]（text + tool_result）；assistant=str | list[dict]（text + thinking + tool_use）；system=str/list。save() 写全部消息（含 system），故 /resume 保留旧 system。
- **写盘工具**：`src/core/fileutil.py:atomic_write_text`；sandbox `safe_path` 约束 workspace。
- **命令登记**：`_SLASH_COMMANDS`（main.py:858）+ `_HELP_TEXT`（:884）+ dispatch if-chain。

## Requirements

- `/export [格式] [路径]`：导出当前对话。
  - `markdown`（默认）：人读，user/assistant 文本 + 工具调用单行摘要 + 工具结果截断；跳过 system；默认不含 thinking。落 `.transcripts/exports/<session>_<ts>.md`。
  - `json`：自包含 wrapper（`{version, session_id, exported_at, messages}`，messages 原样含 system/thinking/工具，保真），可被 /import 读回。
  - 可选显式路径覆盖默认位置；`atomic_write_text` 落盘。
- `/import <路径>`：读外部 `.json`（wrapper 或裸 messages list）/`.jsonl` → 校验 shape（list[dict] 且每条有 role）→ 载入**新会话**（镜像 /resume 机制）。坏文件 → 报错 + 零状态改动（fail-safe）。
- 纯模块 `src/memory/conversation_io.py`：`render_markdown` / `to_export_json` / `parse_import`（含校验），无 REPL 依赖、可单测。
- `_SLASH_COMMANDS` + `_HELP_TEXT` 登记；CLAUDE.md 记录。

## Acceptance Criteria

- [ ] `/export`（无参）→ markdown 落 `.transcripts/exports/`，含 user/assistant、工具摘要、跳过 system、无 thinking。
- [ ] `/export json [path]` → wrapper JSON，messages 保真。
- [ ] `/import <wrapper.json>` / 裸 list / `.jsonl` → 载入新会话、`_replay_stdio_transcript` 重放、新 session id、token_tracker 重置。
- [ ] 坏 import 文件（非 list / 无 role / 坏 JSON）→ 报错 + 当前对话/会话不变、不崩。
- [ ] `conversation_io` 纯函数单测：markdown 渲染各 block、json 往返、parse 校验拒绝非法 shape。
- [ ] pytest 全绿、ruff clean、pyright 0、无新依赖。

## Definition of Done

- 新行为有 pytest（render/serialize/parse + dispatch 失败安全）。
- lint/typecheck/测试全绿。
- CLAUDE.md（会话管理段）+ 命令登记更新。

## Decision (ADR-lite)

**Context**: 现仅 workspace 本地 save/resume，缺便携导入导出；需对齐 Claude Code `/export` 且避免重复造会话切换机制。

**Decision**（用户已确认全部推荐）:
- **D1** `markdown`（默认无参）+ `json`（自包含 wrapper，可 /import 读回）。
- **D2** markdown：user/assistant 文本 + 工具调用单行摘要 + 工具结果截断 + 跳过 system + 默认无 thinking；JSON 全保真。
- **D3** 默认落 `.transcripts/exports/<session>_<ts>.{md,json}`（mkdir + atomic_write_text），`/export [格式] [路径]` 可显式覆盖；不经 PermissionGuard（同 /loop 档），显式路径按用户输入。
- **D4** /import 载入**新会话**（镜像 /resume：换 messages[:]、新 session id、token_tracker.reset、切 mailbox、rebuild handlers、_replay_stdio_transcript 重放）；追加到当前对话 Out of Scope。
- **D5** 接受 wrapper JSON / 裸 messages list / .jsonl（自动判形）；校验 list[dict] 且每条有 role，否则报错 + 零状态改动（fail-safe）。
- **D6** 纯模块 `src/memory/conversation_io.py`（render_markdown / to_export_json / parse_import+校验，无 REPL 依赖、可单测）；main.py dispatch 接 I/O + 会话切换。

**Consequences**: 无新依赖；markdown 便分享、JSON 可跨机器搬运并往返；坏 import 不崩、不污染当前会话；复用 /resume 机制与 _replay_stdio_transcript 渲染，零重复造轮子。

## Out of Scope

- 导入「追加到当前对话」（只做载入新会话，避免 id 冲突/混合）。
- 导出到剪贴板（Claude Code 有；本期只落文件，剪贴板跨平台依赖多，记后续）。
- 导出 HTML / 富格式、对话分享到远端服务。
- 自动定时导出 / 导出全部会话批处理。

## Technical Notes

- 关键文件：`src/memory/conversation_io.py`(新)、`src/main.py`（`_dispatch_export_command`/`_dispatch_import_command` + 命令登记 + dispatch）、`CLAUDE.md`、`tests/test_conversation_io.py`(新)。
- /import 镜像 /resume 机制；/export markdown 复用 `_replay_stdio_transcript` 的遍历结构（抽到纯函数）。
- /export/import 是 REPL 用户主动命令，不经 PermissionGuard（同 /loop 档，基础设施级）；显式路径按用户输入。
