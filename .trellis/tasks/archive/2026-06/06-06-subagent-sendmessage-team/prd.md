# 有状态子代理上下文：subagent SendMessage 续跑 + team 队友记忆

## Goal

让子代理（subagent）从"一次性问答、跑完即弃"升级为"可被追加消息续跑、上下文保持"，对标 Claude Code 的 `SendMessage`——能用 agent ID 继续此前 spawn 的子代理、其上下文原样保留。可选地，把同一"有状态上下文"理念延伸到 team 队友，让队友跨请求有对话记忆（当前每个请求都从零新建 messages、且不压缩）。

## What I already know（代码勘察）

- **subagent**（`src/planning/subagent.py`）：`run_subagent` → `_run_subagent_sync` 每次新建 `messages`（system + user task）跑 `agent_loop`、返回 final 字符串，**messages 跑完即丢**。无 ID 注册表、无续跑入口。后台路径返回 `subagent-<id>` task_id 但结果经 notification 浮现、messages 仍丢。worktree 隔离子代理在 loop 结束 `finally` 里按 dirty 决定保留/清理 worktree。
- **subagent 接线**：`core/tools.py:get_handlers` 里 `handlers["subagent"]` = 闭包调 `run_subagent`（持有 provider/available_tools/handlers/permission/depth/retry_policy）。`subagent` schema 在 `SUBAGENT_TOOL_SCHEMAS`（task/agent_type/run_in_background/isolation）。
- **team 队友**（`src/team/autonomous.py`）：`AutonomousAgent` 是常驻守护线程，但 `_handle_messages`→`_run_prompt` 每个请求**新建 messages**（system + 单条 prompt）、`compact_fn=lambda _:None`**不压缩**——队友对象常驻、对话却无记忆。
- **主循环专属工具机制**：`agent_types.MAIN_LOOP_ONLY_TOOLS = {exit_plan_mode, workflow}` + 不进全局集 + `filter_tools`/`filter_handlers` 三重保障，让子代理永远拿不到这些工具。续跑工具若要"子代理不能续跑别的子代理"也走这套。
- **会话作用域状态**：`spawned_agents` dict（main.py:3022）随 `/new`·`/resume`·`/import` 重置、`/compact` 保留——session-scope 状态的标准宿主。
- **纯模块 + 注入回调可单测范式**：`retry.py`/`goal.py`/`workflow.py` 均为零 LLM/loop/SDK 依赖、注入回调驱动——本任务的注册表/续跑逻辑应同样可单测。

## Assumptions（待校验）

- 续跑工具是主循环专属（子代理不能续跑），与 workflow/exit_plan_mode 同档。
- 续跑上下文是 in-memory、session-scope（不跨 `/resume` 持久化），与 team/todo 一致。
- MVP 续跑只覆盖前台、`isolation="none"` 的子代理；后台 + worktree 续跑因生命周期复杂留后续。

## Decisions (ADR-lite)

- **Q1 = A（仅 subagent 续跑）**。Context：subagent（同步/REPL 作用域）与队友（守护线程）运行时差异大，强行统一是过度设计。Decision：本任务只做 subagent SendMessage 续跑这一头部特性，自洽且直接对标 `SendMessage`。Consequences：队友记忆作为下一个独立任务（需另解无界增长 + 压缩 + 角色交替），见 Out of Scope。

- **Q2 = 1（独立 `subagent_send` 工具）**。Context：续跑时 `subagent` 的 agent_type/isolation/run_in_background 字段全是噪声，且 `subagent` 本身非主循环专属（子代理可嵌套 spawn），而续跑要做成主循环专属。Decision：新增独立 `subagent_send(agent_id, message)` 工具，走 `MAIN_LOOP_ONLY_TOOLS` 三重保障；`subagent` 前台返回里附 agent ID 供续跑引用。Consequences：精确对标 CC `Agent`/`SendMessage` 分工，两 schema 各自干净；代价是多一个工具 + `subagent` 返回需带 ID 脚注。

- **Q3 = A（仅前台 + isolation="none"）**。Context：后台子代理 fire-and-notify、worktree loop 末即拆，两者生命周期都假设"跑完就了结"，与"持有可重入上下文"本质冲突。Decision：只有前台、none-isolation 子代理注册可续跑上下文；带 run_in_background/worktree 的不注册、返回不给续跑 ID。Consequences：与 workflow MVP 一致（先做扎实同步路径），后台/worktree 续跑出 scope。

- **Q4 = 全部按推荐 + 4 条边界**。Decision：注册表 session-scope in-memory（`/new`·`/resume`·`/import`·`/clear` 清空、`/compact` 保留），FIFO 软上限（touch 时 move-to-end，淘汰最旧），上限走 `[subagent] max_resumable`（默认 20，restart-required）。续跑不存在/已淘汰 ID → 清晰 Error never-raise。四条边界：(1) 只在 loop 成功完成时注册（抛错不注册）；(2) 续跑结果用同一 ID 回写更新 messages，支持多轮；(3) 只注册主循环直接 spawn 的子代理（嵌套子代理传 `registry=None` 不注册）；(4) 空 message/空 agent_id → Error。

## Requirements

1. **可续跑注册表（纯模块 + 可单测）**：新增 `src/planning/subagent_registry.py`——`ResumableContext`（agent_id + messages + 重入 `agent_loop` 所需绑定：provider/tools/handlers/permission/compact_fn/max_turns/retry_policy）+ `SubagentRegistry`（`register`/`get`/`has`/`clear` + FIFO 软上限 move-to-end 淘汰 + `generate_id` → `sa-<rand8>`）。CRUD/淘汰逻辑纯净可单测。
2. **前台 none-isolation 子代理注册**：`run_subagent`/`_run_subagent_sync` 加 `registry` 参数。仅当 `registry is not None and isolation == "none"` 且 loop 成功完成时，构造 `ResumableContext` 注册，并在返回字符串尾部附续跑 ID 脚注。后台路径传 `registry=None`（不注册）；嵌套子代理闭包传 `registry=None`（不注册）。
3. **主循环专属续跑工具 `subagent_send(agent_id, message)`**：schema + handler 在 `src/core/handlers/subagent_send.py`；纯逻辑 `run_subagent_send(agent_id, message, *, registry, run_loop)`（校验非空 → 查表，缺失返回 Error → 追加 user 消息 → 注入的 `run_loop(ctx)` 重入 `agent_loop` → move-to-end 刷新位置 → 返回结果 + 续跑 ID 脚注）。入 `MAIN_LOOP_ONLY_TOOLS`，不进 `get_tools()` 全局集，schema append 到 `loop_tools`，handler 经 `_install_subagent_send_handler` 在 boot + 4 个会话切换点安装。
4. **子代理隔离三重保障**：`subagent_send` 入 `MAIN_LOOP_ONLY_TOOLS`（`filter_tools` 对所有子代理类型恒剥离）+ 不在全局集 + `filter_handlers` 丢孤儿 handler——任何子代理（含嵌套、general-purpose）都拿不到 `subagent_send`。
5. **权限档对齐 `subagent`**：`subagent_send` **不入** `SAFE_TOOLS`（续跑会触发写）——DEFAULT 确认 / PLAN 拒绝 / BYPASS 放行。
6. **会话作用域接线**：`subagent_registry` 在 REPL session 实例化一次（紧邻 `spawned_agents`），`.clear()` 于 `/new`·`/resume`·`/import`·`/clear`；穿入 `_build_handlers`→`get_handlers` 让主循环 subagent 闭包持有它。
7. **配置 `[subagent] max_resumable`**（默认 20）：`SubagentConfig` 加字段 + 解析容错 + restart-required（不进 hot 集）。

## Acceptance Criteria

- [ ] `SubagentRegistry` 单测：register/get/has/clear、超上限 FIFO 淘汰最旧、touch（重注册）move-to-end 不被误淘汰、`generate_id` 唯一前缀。
- [ ] `run_subagent_send` 单测（注入 fake registry + fake run_loop）：正常续跑回写 + 脚注、不存在 ID → Error、空 agent_id/空 message → Error、续跑后同 ID 仍可再续。
- [ ] 前台 none-isolation 子代理跑完后可在注册表查到上下文 + 返回带 ID 脚注；后台 / worktree 子代理**不**注册（单测或集成）。
- [ ] 嵌套子代理 spawn 的子代理不注册（registry=None 路径）。
- [ ] `subagent_send` 不出现在任意子代理类型的过滤后工具集（`filter_tools` 剥离）。
- [ ] `subagent_send` 不在 `SAFE_TOOLS`（权限档对齐 subagent）。
- [ ] `[subagent] max_resumable` 解析容错（非法值回退默认）。
- [ ] 全量 pytest 绿；ruff check 干净（仅 format 改动文件）。

## Definition of Done

- 新增行为有 pytest 覆盖（纯模块注册表/续跑逻辑 + 主循环接线）。
- ruff check 干净（只 format 改动文件）。
- 全量 pytest 绿。
- CLAUDE.md 增补子系统段落；config 若新增配置段同步注释。

## Out of Scope（evolving）

- **team 队友记忆**（让 `AutonomousAgent` 跨请求记住对话）：独立后续任务——需同时解决无界增长（接 Compactor，现 `compact_fn=lambda _:None`）、角色交替、跨会话语义。本任务不碰。

## Technical Approach

- **新模块 `src/planning/subagent_registry.py`**（纯，零 LLM/loop 依赖，注入回调可单测，仿 retry.py/goal.py/workflow.py）：`ResumableContext` dataclass + `SubagentRegistry`。`messages` 为可变 list，agent_loop 原地追加，故续跑后自动最新；FIFO 用插入序 dict，register 时 pop+重插（move-to-end）实现"按最近触碰淘汰最旧"，超 `max_resumable` 弹最旧。
- **`subagent.py` 改动**：`run_subagent` + `_run_subagent_sync` 加 `registry` 参数。`run_subagent` 前台路径转发 `registry=registry`、后台 `submit` 的 partial 传 `registry=None`；`_run_subagent_sync` 内嵌套 subagent 闭包传 `registry=None`。注册时机：loop 成功返回后、`isolation == "none" and registry is not None` → 存 `ResumableContext(messages, provider, filtered_tools, child_handlers, child_permission, compact_fn, resolved_type.max_turns, retry_policy)`，footnote 附 `[subagent id: <id> — 用 subagent_send 续跑]`。
- **`core/handlers/subagent_send.py`**（新）：`SUBAGENT_SEND_TOOL_SCHEMA`（agent_id + message 必填）+ `run_subagent_send(agent_id, message, *, registry, run_loop)` 纯逻辑。`run_loop(ctx)` 在 main.py 安装闭包里 = `agent_loop(provider=ctx.provider, messages=ctx.messages, tools=ctx.tools, handlers=ctx.handlers, permission=ctx.permission, compact_fn=ctx.compact_fn, bg_manager=None, max_iterations=ctx.max_turns, retry_policy=ctx.retry_policy)`。
- **`agent_types.py`**：`MAIN_LOOP_ONLY_TOOLS` 加 `"subagent_send"`。
- **`main.py`**：`SubagentConfig.max_resumable`（默认 20）+ 解析容错；`subagent_registry = SubagentRegistry(config.subagent.max_resumable)` 紧邻 `spawned_agents`，`.clear()` 于 /new·/resume·/import·/clear；穿入 `_build_handlers`→`get_handlers`；`loop_tools.append(SUBAGENT_SEND_TOOL_SCHEMA)`；`_install_subagent_send_handler` 仿 `_install_workflow_handler` 在 boot + 4 切换点重装。
- **`core/tools.py`**：`get_handlers` 加 `subagent_registry` 参数，主循环 subagent 闭包传 `registry=subagent_registry`（fallback / provider=None 路径不变）。

## Technical Notes

- 关键文件：`src/planning/subagent_registry.py`(新)、`src/planning/subagent.py`、`src/core/handlers/subagent_send.py`(新)、`src/core/tools.py`（get_handlers）、`src/planning/agent_types.py`（MAIN_LOOP_ONLY_TOOLS）、`src/main.py`（SubagentConfig + registry 宿主 + loop_tools + 安装点 + 会话切换 clear）、`src/permission/guard.py`（确认 subagent_send 不入 SAFE_TOOLS）、`config.toml [subagent]`。
- 对标：Claude Code `SendMessage`（用 agent ID 续跑此前 spawn 的子代理、上下文原样保留；新 `Agent`/`subagent` 调用则全新开始）。
- 范式参照：`_install_workflow_handler`/`_install_plan_handler`（主循环专属工具安装）、`spawned_agents`（session-scope 状态生命周期）、`retry.py`/`goal.py`/`workflow.py`（纯模块 + 注入回调可单测）。
