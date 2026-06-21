# 会话 fork 与树状分支

## Goal

让用户能从当前对话历史的任意一个**干净 turn 边界**分叉出一个新分支会话、独立继续，并保留分支谱系（parent + fork 点），可在树中导航。对标 Pi 的树状 JSONL 会话（/tree、in-place branching）与 Codex 的 fork-from-any-message。补上 BareAgent 当前「所有会话操作都是线性的（resume 整个已存会话 / 开全新会话），没有从过去某条消息分叉，也没有谱系/树结构」这一缺口。

## Confirmed Facts（来自只读探查）

- **存储**：`TranscriptManager`（`memory/transcript.py`）写 `.transcripts/<session_id>_<timestamp>.jsonl`，每行一条 JSON 消息；同一 session_id 可有多个时间戳快照，load 取最新。`session_id` 格式 `<timestamp>-<rand6>`（`main.py:_generate_session_id`，带 `reserved_ids` 去重）。
- **消息不变量**：REPL 持扁平 `messages: list[dict]`，Anthropic role 交替；assistant 的 `tool_use` 块与紧随其后 user 消息里的 `tool_result` 块成对，不能拆开。会话首条通常是 system 消息（render/replay 都跳过）。turn 之间 `messages` 以一条「完整 assistant 回应」结尾（agent_loop 返回后），下个 user 输入再 append。
- **session-switch 标准序列**（`/import` 4093-4143 / `/resume` 4007-4064 / `/new` 3909-3966 共用）：`messages[:]=…` → `token_tracker.reset()` →（新会话才）`_generate_session_id(reserved_ids=…)` → `_set_compact_session_id` → `_set_interaction_logger_session` → `_switch_session_mailbox(current_bus=…)` → `spawned_agents={}` + clear `pending_team_messages`/`pending_workflow_messages`/`subagent_registry`/`workflow_registry`/`recency_tracker` → `_build_handlers(runtime_id=new_sid)` → `_install_plan_handler`/`install_workflow_handler`/`install_subagent_send_handler` → `_replay_stdio_transcript(messages, ui_console)` →（新会话才）`_save_transcript_snapshot`。`/import` 是「新会话 + 立即切换」的最贴近模板。
- **compaction 后结构**：`compact.py:Compactor.__call__` 把 `messages` 重置为 `[system…]+[{user:"[Context Compressed]\n<summary>"},{assistant:"收到…"}]+[pending_user?]`，仍合法 role 交替——压缩摘要就是一个普通 user→assistant turn。
- **复用件**：`conversation_io.render_markdown` / `main._replay_stdio_transcript` 的消息遍历 + `tool_name_by_id` 关联逻辑可参考用于 fork-点预览。
- **架构风格**：纯逻辑模块 + 注入回调可单测（`core/retry.py`、`core/goal.py`、`core/workflow.py`、`planning/skill_gen.py`），副作用留 `main.py`。本特性新建纯模块 `memory/session_tree.py`（fork 点枚举/切分校验 + 谱系树模型 + ASCII 渲染），main.py 只接线 + 复用 session-switch 辅助。

## Decisions（已敲定）

- **A. 选择模型 = 清洁 turn 边界编号**：`/fork`（无参）枚举每个**完整 assistant 回应**为分叉点，编号 1..K，显示「触发它的 user 提示预览 + assistant 回应预览」；`/fork <N>` 按点编号选，slice = 到该 assistant turn 末尾（含完整 tool_use/tool_result 周期）。**由构造保证合法**——不存在拆 tool 对/越界半 turn 的非法情形，无「自动回退」补救分支。「重做第 M 句」= 选其前一个 assistant 点再重新输入。纯模块重点单测：每个 offer 的点 slice 满足 role 交替 + 无悬空 tool_use；越界/无点编号给明确错误。
- **B. fork 源 = 仅当前 live 对话**：`/fork` 列表/切分都基于 REPL 内存里的 `messages`；fork 旧会话 = 先 `/resume <id>` 再 `/fork`。`/fork` 签名极简（只跟点编号），切分对象已在内存且 turn 间结尾干净。
- **C. compaction 交互 = 零特殊处理**：fork 直接切 live `messages`；压缩摘要作为普通 turn 被枚举/切分，无需任何分支。
- **D. 谱系存储 + /tree 范围 = 只记 fork 边 + 全森林**：`.transcripts/.tree.json` = `{child_session_id: {parent, fork_point, created}}`，**只在 `/fork` 写**一条 child→parent；`/new`/`/import`/首启不写（隐式根）。fail-open：缺失/损坏 → 退化平铺、不崩。`/tree` 节点 = `list_sessions()` 全部会话，边 = tree.json fork 关系，无 parent = 根，标当前节点。`fork_point` 存「点编号 N + 父切片消息数」，仅供显示「forked from <parent> @ turn N」，不参与重建。
- **E. /tree 导航 = 纯展示 + 复用 `/resume <id>`**：`/tree` 只渲染会话树（节点带 session-id、谱系、当前标记）；切换用既有 `/resume <id>`（id 在树里可见可复制）。零新切换路径，`/tree resume`/`/tree <id>` 留作扩展位。

## Requirements

1. **`/fork` 命令**：`/fork`（无参）列出当前对话的合法分叉点（编号 1..K + user/assistant 预览）；`/fork <N>` 按编号在该点分叉。
2. **截断边界合法性（关键正确性，纯模块重点单测）**：fork 产出 `messages[0:cut]` 的**深拷贝**，cut 由点编号 N 映射到对应 assistant turn 末尾；结果保持 role 交替、无悬空 tool_use、能接下一个 user turn。越界/非整数/无可分叉点 → 明确错误、零状态改动。
3. **谱系存储**：sidecar `.transcripts/.tree.json`（`{child:{parent,fork_point,created}}`），原子写，fail-open（缺失/坏文件不崩）。不污染纯 JSONL 消息格式（不往 jsonl 塞 metadata）。
4. **fork 后立即切换**：镜像 `/import` 完整 session-switch 序列、复用其辅助函数；新分支深拷贝、独立可变状态；末尾 `_save_transcript_snapshot` 落盘新会话 + 写谱系。
5. **状态重置**：完全镜像 `/import`（token_tracker / spawned_agents / pending_team / pending_workflow / subagent_registry / workflow_registry / recency 全重置）。
6. **`/tree` 命令**：ASCII 树（parent→children、标当前节点、标 fork 点 `@ turn N`），节点 = 全部会话森林；纯展示，靠 `/resume <id>` 导航。fail-open。
7. **命令登记**：`/fork`、`/tree` 进 `_SLASH_COMMANDS` + `_HELP_TEXT`（补全列表）。

## Acceptance Criteria

- [ ] 纯模块 `session_tree.py`：`enumerate_fork_points(messages)` 返回合法点列表（每点带 cut 索引 + user/assistant 预览）；`slice_for_fork_point(messages, n)` 返回深拷贝合法前缀或抛/返回错误；单测覆盖「含多轮 tool_use/tool_result 周期」「含 compaction 摘要轮」「越界 N」「无可分叉点（仅 system / 仅一轮未完成）」。
- [ ] 纯模块谱系：`SessionTree` 模型 + `load_tree`/`record_fork`（原子写、坏文件 fail-open）+ `render_tree(sessions, tree, current)` ASCII 输出；单测覆盖「多层 fork」「孤儿/缺失 parent」「坏 json 退化」「当前节点标记」。
- [ ] `/fork` 无参列出合法分叉点；`/fork <N>` 深拷贝切分、镜像 `/import` 切到新分支会话、谱系写入 sidecar、replay + 落盘；非法 N 明确报错且零状态改动。
- [ ] `/tree` 渲染全会话森林 + 谱系 + 当前节点；坏/缺失 tree.json 退化为平铺不崩。
- [ ] `/fork`、`/tree` 进 `_SLASH_COMMANDS` + `_HELP_TEXT`。
- [ ] 过本地总闸 `bash scripts/ci-check.sh`（ruff check → format --check → pyright standard → pytest），新代码无新增 pyright error、无全局降级。
- [ ] CLAUDE.md「### 会话管理」小节补一段会话 fork/树状分支说明（Docs commit）。

## Out of Scope（明确不做）

bookmarks、filter、树导出 HTML/gist、跨会话搜索、原地编辑历史消息、分支合并、fork 任意磁盘会话（先 resume 再 fork）、`/tree` 内联切换子命令。
