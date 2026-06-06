# 完善 team/AutonomousAgent 子系统

## Goal

`src/team/` 是 BareAgent 目前最不完整的子系统（CLAUDE.md 里仅一行，无配置段、无详细文档、覆盖度远低于 retry/cache/goal/workflow）。本任务把它从"半成品"补到"LLM 真正能用的协作闭环"，优先解决功能硬伤与健壮性，性能/打磨次之。

## What I already know（代码勘察结论）

核心链路：`autonomous.py`（守护循环）/ `mailbox.py`（JSONL 邮箱）/ `protocols.py`（请求-响应 FSM）/ `manager.py`（队友定义持久化）+ `main.py` 接线（`_make_team_handlers` / `_team_spawn` / `_drain_team_mailbox`）。

已确认的缺口（按影响排序）：

1. **请求-响应回路未闭合到 LLM（功能硬伤）**
   - `_team_send`（`main.py:1692`）发出 request 后立即返回 `"Sent message <id>"`，不等回复。
   - 队友 `_protocol.respond()` 把 response 发回 main 邮箱，但 main 只有 `_drain_team_mailbox`（`main.py:2020-2037`）把它 `print_status` 给**人看**，**从不回灌进 LLM messages**。
   - 后果：调 `team_send` 的 LLM 永远看不到队友回答，无法据此推理。对比 subagent（同步结果）/ workflow（聚合回灌），team 是唯一"发出去就断线"的。
   - 无 `team_receive` / 阻塞等回复工具；`ProtocolFSM.wait_response`（`protocols.py:35`）能力已造好但未暴露成工具。

2. **队友无记忆（与 subagent SendMessage 续跑同源）**
   - `_run_prompt`（`autonomous.py:109-122`）每条消息/任务都构造全新 `messages=[system?, user=prompt]`，跑完即弃，`compact_fn=lambda _: None`。
   - 一个号称"长期协作"的守护代理两条消息之间零上下文。

3. **健壮性：一条坏消息杀死队友**
   - `_execute_task` 包了 try/except；但 `_handle_messages`→`_run_prompt`（请求路径）没有 → 请求里 `agent_loop` 抛异常会穿出 `run()`、后台线程死、队友静默停摆且无重启。
   - `team_list` 仍报 `"running": name in spawned_agents`（`main.py:1687`），`spawned_agents` 只在会话重置时清空，从不剔除崩掉的队友 → 状态与现实脱节。
   - 请求方此时在 `wait_response` 静默挂满 60s（硬编码）超时。
   - 无单队友停止：只有 `_broadcast_team_shutdown`（全员、会话级），没有 `team_shutdown <name>`。

4. **半接线 / 缺口**
   - `Protocol.PLAN_APPROVAL` 是死代码：接收侧 `_build_incoming_prompt`（`autonomous.py:130`）处理了，但全仓库无任何地方真正发出 PLAN_APPROVAL 请求。
   - 无法让 LLM 创建队友：`TeammateManager.register` 既非工具也非 slash 命令，队友只能手改 `.team.json`，LLM 只能 spawn 预定义的。

5. **性能 / 配置**
   - 守护循环（`autonomous.py:44-66`）只 `time.sleep(poll_interval)` 忙轮询，忽视邮箱已有的 `wait_for_message`（条件变量阻塞，`mailbox.py:175`）→ 每条消息最多 1s 延迟 + 无谓唤醒。
   - 邮箱 O(n²)：JSONL 只追加从不轮转，`receive()`（`mailbox.py:73-98`）每次轮询从头读+解析整文件找 `since_id`。
   - 无 `[team]` 配置段：`poll_interval=1.0`、`wait_response timeout=60` 全硬编码。

## Assumptions（待验证）

- team 子系统值得继续投入（vs. 已 ship 的 subagent/workflow 已覆盖大部分多代理需求）；其独特价值=长生命周期自治代理 + 任务认领守护 + 双向消息。
- 优先级：功能硬伤（回路闭合）> 健壮性 > 半接线 > 性能/配置。

## Decision (ADR-lite)

**Q1（方向/范围）= A：聚焦修 team 自身，让它"真正可用"。**
- Context：team 头号问题是"LLM 拿不到队友回复"，是让整个子系统对 LLM 不可用的功能硬伤，修复范围集中、风险低。
- Decision：范围锁在 `team/` + `main.py` team 接线；核心 = 闭合请求-响应回路 + `team_receive` + 异常隔离 + 单队友停止。
- Consequences：队友"有状态记忆/续跑"（原方向 B 核心）**不纳入本任务**，因其本身够大且与 subagent SendMessage 续跑深度耦合，拆成独立的下一个任务设计。

## Decision — Q2（回路闭合形态）= 3（混合，以形态 1 为主体）

- `team_send` 升级为**阻塞式**：内部调已有 `ProtocolFSM.wait_response`，阻塞到回复或超时，回复作 tool_result 直接返回 LLM（复用现成能力，最直接的问答闭环）。
- `_drain_team_mailbox`（`main.py:2020`）从"只 print 给人"升级为"**也回灌 LLM messages**"，让迟到/未经请求/广播消息也进入 LLM 视野。
- 两条通路都闭合到 LLM；引入合理可配 `timeout` 默认。
- 形态 2 的纯异步双步协议（显式 team_receive）对单人工具偏重，留作后续。

## Decision — Q3（健壮性范围）= a+b+c 全纳入

- **(a) 异常隔离**（必做，Q2 直接配套）：给 `_handle_messages`→`_run_prompt` 请求路径包 try/except，出错回一条 **error response**（而非沉默），队友继续存活。阻塞的 `team_send` 因此能立刻拿到"队友处理失败：<原因>"而非干等满超时。
- **(b) 单队友停止 `team_shutdown <name>`**（必做）：向目标队友发 SHUTDOWN 协议消息（接收侧 `autonomous.py:71` 已处理）+ 从 `spawned_agents` 剔除。
- **(c) 活性检测**（纳入）：**代码勘察发现** BackgroundManager 无现成公开存活 API（`_threads` 私有），但加只读 `is_running(task_id)` 访问器仅约 5 行、复用现有 lock+`is_alive()`、不动并发语义；`team_list` 改查 `is_running(f"team:{runtime_id}:{name}")` 而非只看 `spawned_agents` 字典在不在。判定足够便宜 → 纳入。

## Decision — Q4（剩余项 MVP 边界）

- **纳入**：`wait_for_message` 替换守护循环忙轮询（提升阻塞问答响应速度，复用 `mailbox.py:175` 现成原语）；`[team]` 配置段（`poll_interval` / `response_timeout`，与 retry/cache/goal/workflow 仓库惯例一致）。
- **Out of Scope**：PLAN_APPROVAL 接线（需给 team_send 加 protocol 参数 + 缺真实场景，留着无害不删不接）；`team_register` 工具/命令（MVP 手改 `.team.json` 即可）；邮箱 O(n²) 轮转（性能优化非可用性，轮转有正确性风险）。

## Requirements

仅触及 `src/team/` + `src/main.py` team 接线 + 必要的 `core/tools.py`/`schema`/`permission` 登记 + `concurrency/background.py` 只读访问器。

1. **回路闭合（Q2=3）**
   - `_team_send`（`main.py:1692`）升级为**阻塞式**：发出 request 后用 `ProtocolFSM(bus, MAIN).wait_response(msg_id, timeout=team.response_timeout)`（复用 `protocols.py:35`）等回复，把回复内容作 tool_result 返回 LLM；超时返回明确"队友 N 秒内无响应"提示。
   - 目标若为 `MAIN_AGENT_NAME` 或队友未在运行（经 Q3c 的 `is_running` 预判），**不阻塞**、立即返回清晰说明（避免干等满超时）。
   - `_drain_team_mailbox`（`main.py:2020`）从"只 `print_status`"升级为"**也回灌 LLM messages**"，让迟到/未经请求/广播消息进入 LLM 视野。
   - **两条通路去重**：`team_send` 阻塞消费的 response 不能被 `_drain` 重复回灌——用 message-id 去重集（team_send 把已消费 response id 记入，drain 跳过已记入 id）协调两条通路对同一 MAIN 邮箱的读取。

2. **异常隔离（Q3a）**：`_handle_messages`→`_run_prompt` 请求路径包 try/except；出错经 `_protocol.respond(message.id, "[error] ...")` 回 error response + 记日志，队友存活继续循环。

3. **单队友停止（Q3b）`team_shutdown <name>`**：新增工具 + slash 命令（镜像 team_send 的注册/暴露/权限档），向目标发 SHUTDOWN 协议消息（接收侧 `autonomous.py:71` 已处理，SHUTDOWN 检查在 msg_type 过滤之前）+ 从 `spawned_agents` 剔除。

4. **活性检测（Q3c）**：`concurrency/background.py` 加只读 `is_running(task_id) -> bool`（lock-guarded，复用 `is_alive()`）；`team_list`（`main.py:1683`）改用 `is_running(f"team:{runtime_id}:{name}")` 反映真实存活，而非只看 `spawned_agents` 字典。

5. **响应速度（Q4）**：`AutonomousAgent.run`（`autonomous.py:44-66`）两处 `time.sleep(poll_interval)` → `self.bus.wait_for_message(self.name, timeout=poll_interval)`：有信立刻醒、无信仍周期醒来查任务。

6. **配置（Q4）`[team]` 段**：`TeamConfig(poll_interval=1.0, response_timeout=60.0)` + `_parse_team_config` 逐字段容错；`poll_interval` 喂 `_team_spawn` 的 AutonomousAgent、`response_timeout` 喂 `_team_send` 等待；boot 固化、restart-required（generic `_flatten_config` 自动归 restart）。

## Acceptance Criteria

- [ ] `team_send` 阻塞等回复，回复作为 tool_result 返回 LLM；队友 N 秒内无响应返回明确超时提示。
- [ ] `team_send` 目标为 MAIN 或未运行的队友时立即返回清晰说明（不阻塞满超时）。
- [ ] 队友请求处理抛异常被隔离：回 error response、队友存活；阻塞的 `team_send` 拿到 error 而非干等超时。
- [ ] `team_send` 阻塞消费的 response 不被 `_drain` 重复回灌 LLM（去重生效）。
- [ ] `_drain_team_mailbox` 把迟到/未经请求的回复也回灌 LLM messages（不再只 print）。
- [ ] `team_shutdown <name>` 工具 + slash 命令：停单个队友 + 从 `spawned_agents` 剔除。
- [ ] `team_list` 经 `BackgroundManager.is_running` 反映真实存活，崩掉的队友显示 not running。
- [ ] 守护循环用 `wait_for_message` 唤醒，新消息延迟 < poll_interval（不再固定睡满）。
- [ ] `[team]` 配置段（poll_interval / response_timeout）逐字段容错、restart-required。
- [ ] 纯逻辑可单测部分有 pytest 覆盖；ruff check/format（仅改动文件）green；CLAUDE.md 补 team 子系统详细段。

## Definition of Done

- 新增/更新 pytest 测试（纯逻辑层可单测，仿 retry/goal/workflow 范式）
- ruff check / format（仅改动文件）green
- CLAUDE.md 补 team 子系统详细段（对齐其它特性的文档密度）
- 配置项（若引入）写入 config.toml + 文档

## Out of Scope

- **队友有状态记忆 / SendMessage 续跑**（原方向 B 核心）——拆成独立的下一个任务，与 subagent 续跑统一设计。
- **PLAN_APPROVAL 协议接线**（接收侧已存在，发送侧不接；不删不接）。
- **`team_register` 动态创建队友**（MVP 手改 `.team.json`）。
- **邮箱 O(n²) 轮转 / 截断 / offset 游标**（性能优化，留后续）。
- **显式异步 `team_receive` 双步协议**（Q2 形态 2，留后续）。

## Technical Approach

按方向 A（仅修 team 自身）落地 Q1-Q4：

- **回路闭合**：`_team_send` 改阻塞 + `_drain_team_mailbox` 改"也回灌 messages" + message-id 去重集协调两条通路。去重集随 MAIN 邮箱读取游标一起在 REPL 会话作用域维护（`/new`·`/resume`·`/import` 重置）。
- **健壮性**：请求路径 try/except 回 error response（autonomous.py）；`team_shutdown` 工具+命令（main.py team handlers + tools.py schema + slash dispatch）；`BackgroundManager.is_running` 只读访问器 + `team_list` 改用之。
- **响应/配置**：`wait_for_message` 替换守护循环忙轮询；`TeamConfig` + `_parse_team_config` + 两处注入。
- **可测性**：纯逻辑（去重、is_running、_parse_team_config、wait_for_message 唤醒、异常隔离回 error response）注入回调/用真实 MessageBus 单测，仿 retry/goal/workflow 范式。
- **文档**：CLAUDE.md 新增 "### 多智能体协调" 的 team 详细段（对齐其它特性文档密度）；config.toml 补 `[team]`。

## Technical Notes

- 关键文件：`src/team/{autonomous,mailbox,protocols,manager}.py`、`src/main.py`（team 接线 `_make_team_handlers`/`_team_spawn`/`_drain_team_mailbox`/`_broadcast_team_shutdown`）、`src/core/tools.py`（team_* schema/fallback）、`src/permission/guard.py`（`team_list` 入 SAFE_TOOLS）。
- 范式参考：纯逻辑模块 + 注入回调可单测（`retry.py`/`goal.py`/`workflow.py`）；主循环专属工具隔离三件套（`exit_plan_mode`/`workflow`）；fail-closed 后台权限（`permission.clone(fail_closed=True)`）。
