# team 队友有状态记忆（跨请求对话续跑 + Compactor）

## Goal

让 team 队友（`AutonomousAgent`）在**跨请求**之间保留对话上下文，而不是当前的"逐请求无状态、跑完即弃"。这是方向 B 的另一半——子代理 SendMessage 续跑（task 06-06-subagent-sendmessage-team）补齐了"前台子代理可续跑"，本任务把同一个 SendMessage 母题落到 team 队友上：`team_send X 做 A → 追问基于 A 的 B` 时，队友 X 记得 A 的上下文。

## What I already know

- `src/team/autonomous.py:_run_prompt`（116–129）每次都现建 `messages: list = []`（system + 单条 user），`compact_fn=lambda _messages: None` → 队友逐请求无状态。
- `_run_prompt` 有**两个调用点**：`_handle_messages`（收到 team_send 的 request，对话型）+ `_execute_task`（自认领 TaskManager 的 ready task，离散工作单元）。
- `_handle_messages` / `_execute_task` 都已包 try/except，单条请求/任务失败不杀守护线程（06-06-team-subsystem-completion 已硬化）。
- `Compactor`（`src/memory/compact.py`）= 现成可注入 callable：`Compactor(provider, transcript_mgr, threshold=50000, session_id)`，`__call__(messages, force=False)` 原地压缩，按 `estimate_tokens > threshold` 触发，保留 system 消息 + pending user turn。主循环用 `base_compact_fn = Compactor(provider, transcript_mgr, session_id)`（main.py:3107）。
- 每个队友是**独立 daemon 线程**（`bg_manager.submit(task_id, autonomous_agent.run)`），`run()` 单线程串行处理 mailbox/task → `self._messages` 只被该线程读写，**无需额外锁**。
- 角色交替天然成立：每个 `_run_prompt` append 一条 user，agent_loop 跑完收在 assistant turn → system, user, assistant, user, assistant... 只要 system 改为 init 一次性加入（而非每次现建）。
- `_team_spawn`（main.py:1792）构造 `AutonomousAgent(name, provider, tools, handlers, bus, task_manager, permission, system_prompt, poll_interval)`；provider 来自 `teammate_manager.spawn`，每队友独立。
- `[team]` 配置现有 `poll_interval=1.0` / `response_timeout=60.0`，`_parse_team_config` 逐字段容错，restart-required。

## Assumptions (temporary)

- 记忆是**进程内、随队友线程生命周期**的（线程结束 = 记忆消失），不跨 `/resume` 持久化到磁盘。
- Compactor 注入而非内建（对齐全仓"注入回调可单测"范式），AutonomousAgent 默认 no-op 保持向后兼容 + 测试简单。

## Open Questions

- ~~Q1（记忆范围）~~ → **已定 (a)**：仅 request 续记忆，自认领 task 保持无状态。
- ~~Q2（Compactor 注入 + 配置）~~ → **已定 (a)**：注入 compact_fn（默认 no-op）+ `[team] memory_enabled` 开关（默认 ON + env），阈值复用默认。
- ~~Q3（失败回滚）~~ → **已定 (a)**：snapshot + `del self._messages[snapshot:]` on exception，复刻 /goal 回滚范式。

## Decision (ADR-lite)

### Q1 — 记忆范围（已定）
- **Context**：队友有两个 `_run_prompt` 入口（request 对话型 / 自认领 task 离散型）。
- **Decision**：(a) 仅 request 路径累积 `self._messages`；自认领 task 仍现建 `messages=[]` 跑完即弃。
- **Consequences**：契合"对话追问"价值点、改动面只在 request 路径、task 执行保持干净自包含；代价是 task 之间不互相记忆（符合 task 离散单元语义，可接受）。

### Q2 — Compactor 注入 + 配置（已定）
- **Context**：Compactor 是现成可注入 callable；记忆是行为变更（成本/语义变化）。
- **Decision**：(a) 给 `AutonomousAgent` 加 `compact_fn` 参数（默认 no-op）；`_team_spawn` 建 per-teammate `Compactor(provider=队友provider, transcript_mgr=None, session_id=f"team:{name}")` 注入。加 `[team] memory_enabled`（默认 ON + env `BAREAGENT_TEAM_MEMORY_ENABLED`）作 kill switch；关掉时 `_team_spawn` 注入 no-op compact_fn 且不累积（回退今天的无状态）。阈值不单独暴露（与主循环 base_compact_fn 一致，复用默认 50000）。
- **Consequences**：对齐全仓注入范式、AutonomousAgent 与 Compactor 解耦可单测；留一个回退开关；阈值 YAGNI 后置。

### Q3 — 失败回滚（已定）
- **Context**：带记忆的 request 路径中 agent_loop 抛异常会在 `self._messages` 残留孤儿 user + 半截 assistant，破坏角色交替。
- **Decision**：(a) 进 turn 前 `snapshot = len(self._messages)`，append user 后跑 loop，`except BaseException: del self._messages[snapshot:]; raise`。`_handle_messages` 现有 try/except 照旧 respond 错误文本。
- **Consequences**：一次瞬时失败不毒化记忆线，下条 request 从干净的 assistant 边界续；复刻 /goal `_drive_goal` 回滚 + subagent_send "成功才注册" 安全范式。

## Implementation 设计要点（实现细节，非决策）

- `AutonomousAgent.__init__`：新增 `compact_fn` 参数（默认 `lambda _messages: None`）+ `memory_enabled: bool = False`（向后兼容默认关）。init 时若 `memory_enabled` 则建 `self._messages`，把 system_prompt 一次性加入。
- request 路径拆出**有状态**变体：append user 到 `self._messages` → snapshot/回滚 → `agent_loop(messages=self._messages, compact_fn=self._compact_fn)`；`memory_enabled=False` 时走旧的无状态 `_run_prompt`（现建 messages）。
- 自认领 task 路径**永远**走无状态 `_run_prompt`（现建 `[{system},{user}]`），不受 `memory_enabled` 影响（Q1）。
- `_team_spawn`（main.py）：`memory_enabled = config.team.memory_enabled` 时建 per-teammate `Compactor(...)`，传 `compact_fn=` + `memory_enabled=True`；否则传默认 no-op + False。
- 线程安全：`self._messages` 仅被该队友单线程读写，无需锁。

## Requirements

- `memory_enabled=True` 时，队友收到的**连续 request** 累积进一条 live `self._messages`，agent_loop 原地追加 turn，下条 request 续在其后。
- Compactor 经 `compact_fn` 注入（默认 no-op），按 token 阈值触发，防无界增长。
- system_prompt 在 init 时加入 `self._messages` 一次（有状态路径不再每次 prepend）。
- request 路径失败安全：snapshot + 回滚，单次 turn 抛异常不破坏角色交替不变量。
- 自认领 task 路径保持无状态（Q1），不受 `memory_enabled` 影响。
- `[team] memory_enabled`（默认 True，env `BAREAGENT_TEAM_MEMORY_ENABLED`，restart-required）；关掉回退今天的逐请求无状态行为。

## Acceptance Criteria

- [ ] `memory_enabled=True` 下连续两条 request 到同一队友，第二条 turn 的 messages 包含第一条的 user+assistant 上下文。
- [ ] `memory_enabled=False`（默认构造）下行为与今天字节级一致：每次现建 messages、no-op compact、不累积。
- [ ] 注入的 compact_fn 在每次有状态 request 的 agent_loop 内被调用（用假 compact_fn 断言被调用 + 收到 self._messages）。
- [ ] 有状态 request 路径 agent_loop 抛异常后 `self._messages` 回滚到 turn 前长度，下条 request 不因脏状态崩溃；异常仍上抛（`_handle_messages` respond 错误文本不变）。
- [ ] 自认领 task 路径无论 `memory_enabled` 真假都现建 messages、不污染 `self._messages`。
- [ ] `_parse_team_config` 容错解析 `memory_enabled`（非法值回退默认）+ env 覆盖生效。
- [ ] 新增行为有 pytest 覆盖（注入假 agent_loop / 假 compactor，纯逻辑可测）。
- [ ] ruff 干净，全量测试绿。

## Technical Approach

核心改动在 `src/team/autonomous.py`（记忆列表 + 注入 compact_fn + memory_enabled 开关 + system init + snapshot 回滚）与 `src/main.py`（`TeamConfig.memory_enabled` + `_parse_team_config` + env 覆盖 + `_team_spawn` 按开关建 per-teammate Compactor 并注入）。`Compactor` 直接复用不改。注入式纯逻辑设计 → 单测用假 agent_loop / 假 compactor 覆盖有状态累积、无状态回退、压缩调用、失败回滚四条主路径。

## Definition of Done (team quality bar)

- Tests added/updated（注入式单测，仿 retry/goal/subagent_send 范式）
- Lint / typecheck / CI green
- CLAUDE.md 的 team 小节补充本特性段落
- 配置项（若有）写入 config.toml + 文档

## Out of Scope (explicit)

- 跨 `/resume` 把队友记忆持久化到磁盘（进程内、随线程生命周期）。
- 每个 requester（from_agent）独立记忆线（单线程单记忆足够 MVP）。
- 队友 transcript 落盘（Compactor 的 transcript_mgr 传 None）。
- 主代理侧 mailbox drain 的记忆（drain 只是把迟到消息前缀注入下个 user turn，与队友自身记忆正交）。
- PLAN_APPROVAL 协议本身的改动（已存在，不动）。

## Technical Notes

- 关键文件：`src/team/autonomous.py`（记忆列表 + 注入 compact_fn + system init + 回滚）、`src/main.py`（`_team_spawn` 构造 per-teammate Compactor 并注入 + 可能的 `[team]` 配置）、`src/memory/compact.py`（复用，不改）。
- 回滚范式参考 `/goal` 的 `_drive_goal`（`del messages[snapshot:]`）与 subagent_send 的"成功才注册"。
- 注入范式参考 retry.py / goal.py / subagent_registry.py：纯逻辑 + 注入回调，主 wiring 在 main.py。

## Open Questions（详见 brainstorm Q&A，逐个拍板后落到 Decision）
