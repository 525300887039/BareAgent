# Workflow 扩展：后台执行 + /workflows 面板 + resume + token budget

## Goal

把 `workflow` 工具（确定性 DAG 编排，task 06-06）补上四个在 MVP 主动砍掉的扩展位：
**后台执行**（不阻塞主循环）、**/workflows 面板**（列出/查看运行中与已完成的 workflow）、
**resume**（复用上次已完成节点的结果，只重跑变更/失败/新增节点）、**token budget**
（给一次 workflow 设 token 上限，超限后停止派发新节点）。对标 Claude Code 的 `Workflow`
工具的后台运行 + `/workflows` 实时面板 + `resumeFromRunId` + `budget` 语义，但落到 BareAgent
的声明式 DAG 模型上（非可执行脚本）。

## Requirements

### R1 后台执行（opt-in flag，默认同步）
- `workflow` 工具 schema 加可选 `run_in_background: bool`（默认 false）。默认走现状同步阻塞路径，
  **行为字节级一致**（向后兼容）。
- `run_in_background=true` 时：经 `bg_manager.submit` 在 daemon 线程跑整个 `run_workflow`，
  工具**立即返回** `Workflow <run_id> started in the background.`，不阻塞主循环。
- 后台 workflow 完成时把**完整聚合 summary**（不走通用 500 字截断）经 workflow 专用 drain
  在下个 user turn 前注入 LLM（镜像 `_drain_team_mailbox` + `pending_*` 缓冲范式）。
- 线程安全：后台线程**绝不碰 console/messages**，只更新带锁的 registry；面板渲染由主线程做。
  同步模式下 `on_progress` 仍走 `console.print_status`（主线程，如现状）。

### R2 /workflows 面板（纯人看 REPL 命令）
- `/workflows`（无参）= list + 用法提示；`/workflows list` = 列出本会话所有 run（run id、
  status running/done、节点计数 done/failed/skipped/running、token 花费、起始相对时间）。
- `/workflows <id>` = 单 run 逐节点详情（id/status/phase/label + 产物或错误截断预览）。
- `/workflows clear` = 清掉已完成（非 running）run 记录释放内存。
- 命令登记进 `_SLASH_COMMANDS` + `_HELP_TEXT` + dispatch；never-raise。
- **不**给 LLM 暴露查询工具（结果靠 R1 异步回灌）；**不**做 cancel（与后台子代理"不可中途取消"一致）。

### R3 resume（LLM 驱动，缓存复用未变节点）
- `workflow` 工具 schema 加可选 `resume_from: <run_id>` 字段。引擎据上次 run 的节点结果计算复用集：
  缓存命中 = 上次同 `id` 节点存在 **且** 状态 `DONE` **且** 原始 `prompt` 文本 hash 未变（不含 upstream 注入）→
  直接复用上次 output（不重跑、不计 budget）。
- 重跑判据：`FAILED`/`SKIPPED`/新增 id/prompt 改动 → 重跑。**保守级联**：任一节点要重跑，则其所有
  transitive dependents 即使 prompt 未变也强制重跑（上游产物已变，缓存下游失效）。
- `resume_from` 指向不存在的 run（registry 已 clear / 跨会话）→ **fail-open 整图重跑**（不报错）。
- 纯逻辑可单测：注入上次结果字典 + 新 spec → 算出复用集/重跑集（含级联）。

### R4 token budget（tool 字段 + config 默认，层间软护栏）
- `workflow` 工具 schema 加可选 `token_budget: int` 字段；`[workflow] default_token_budget` config 兜底
  （默认 0/None = 不限，向后兼容），tool 字段优先于 config。
- 计量管线（新增）：每个 workflow run 建一个共享 `TokenTracker`，透传进每节点 `agent_loop`
  （`run_subagent` 加 `token_tracker` 参数 → `_run_subagent_sync` → `agent_loop`），
  `tracker.total_tokens` 作已花费量。复用的缓存节点不计入（没真跑）。
- 强制点 = **每层启动前**检查 `total_tokens >= budget`：已超则剩余 PENDING 节点全标 `SKIPPED`
  (reason="token budget exhausted")、不启新层；在跑的层正常跑完不杀线程。summary/面板标注预算耗尽。
  **层粒度**（软护栏，对标 CC 调用边界检查，非 mid-call 硬切断）。

### R5 WorkflowRegistry（面板 + resume 的共享后端）
- 内存进程级 + 会话作用域：`/new`·`/resume`·`/import` clear、`/compact` 保留
  （与 SubagentRegistry/Scheduler/spawned_agents 严格一致）。
- 记录每个 run：run_id（`wf-<rand8>`，复刻 subagent id 范式）、spec、各节点 NodeResult（live）、
  token 花费、status、started 时间、完成后的完整 summary、是否已投递（去重 R1 注入）。
- 带锁（后台线程写 + 主线程读）。FIFO 软上限 `[workflow] max_runs`（默认 50），超出裁剪最旧
  （复刻 `SubagentRegistry.max_resumable`）。
- live 进度：`run_workflow` 加可选 `on_node_status(node_id, NodeResult)` 回调，节点状态落定即更新
  registry（locked），面板读到实时状态。

## Acceptance Criteria

- [ ] 不传新字段时 `workflow` 行为与现状字节级一致（同步阻塞 + 完整 summary tool_result）。
- [ ] `run_in_background=true` 立即返回 run_id 不阻塞 REPL；完成后完整 summary 在下个 turn 回灌 LLM。
- [ ] `/workflows` 列出本会话 run + 各节点状态；`/workflows <id>` 看逐节点详情；`/workflows clear` 清完成项。
- [ ] resume：未变 DONE 节点复用、`FAILED`/改动/新增重跑、重跑节点下游级联重跑；`resume_from` 失效则整图重跑。
- [ ] token budget 超限后剩余节点标 SKIPPED 不再启新层，summary 标注；缓存节点不计入花费。
- [ ] 后台线程不碰 console/messages（线程安全）；session 切换 clear registry，在飞后台 workflow 继续跑。
- [ ] `pytest` 新增覆盖：resume 复用/级联计算、budget 层间截断、registry 状态机/FIFO 上限、纯逻辑边界。
- [ ] `[workflow]` 配置 `default_token_budget`/`max_runs` 容错解析不崩 boot；env 旋钮对齐现有范式。

## Definition of Done

- 纯逻辑进 `core/workflow.py`（resume 计算 / budget 截断 / on_node_status 回调，注入可单测）；
  registry + 后台 drive + 面板 + drain 等副作用留 `main.py`。
- `ruff check src tests` 干净；只 `ruff format` 改动文件（勿全树）；源码不带 emoji。
- 线程安全：后台线程只更新带锁 registry，UI 主线程渲染（遵 Scheduler 铁律）。
- CLAUDE.md 的 "Workflow 确定性编排" 段落补充四扩展说明 + 关键文件；`config.toml [workflow]` 补新字段。

## Technical Approach

**纯逻辑层（`core/workflow.py`）**
- 新增 resume 计算纯函数：`compute_resume_plan(spec, prior_results) -> (reuse: dict[id,NodeResult], rerun: set[id])`，
  含 prompt-hash 比对 + transitive-dependents 级联失效（复用 `_find_cycle` 同款图遍历思路）。
- `run_workflow` 扩展：接 `prior_results`（预置复用节点为 DONE，跳过执行）、`token_budget` + `tokens_spent`
  回调（每层前检查超限 → 标剩余 SKIPPED）、`on_node_status` 回调（live 更新 registry）。保持注入式可单测。
- `format_summary` 标注 budget 耗尽 / resume 复用计数。

**副作用层（`main.py`）**
- `WorkflowRegistry`（带锁 + FIFO max_runs + `wf-<rand8>` id）：宿主在 REPL 实例化一次（紧邻
  `spawned_agents`/`subagent_registry`），会话切换 clear。
- `_install_workflow_handler` 扩展：解析 `run_in_background`/`resume_from`/`token_budget`；
  同步路径如旧；后台路径 `bg_manager.submit(run_id, _drive_workflow_run, ...)` 立即返回 id。
- `_drive_workflow_run`：建共享 TokenTracker、查 registry 取 prior_results（resume）、调 `run_workflow`
  （`on_node_status` → registry locked 更新；后台模式 `on_progress` 不碰 console）、完成写 summary + 标未投递。
- `_drain_workflow_results`：镜像 `_drain_team_mailbox`，从 registry 取已完成未投递 run，完整 summary 注入
  下个 user turn；投递后标记去重。
- token_tracker 透传：`run_subagent`/`_run_subagent_sync` 加 `token_tracker` 参数 → 内层 `agent_loop`。
- 面板 `_handle_workflows_command`：list/detail/clear，never-raise，读 registry（locked snapshot）。
- 配置：`WorkflowConfig` 加 `default_token_budget`/`max_runs`；`_parse_workflow_config` 容错；
  env 旋钮对齐（如 `BAREAGENT_WORKFLOW_DEFAULT_TOKEN_BUDGET`/`BAREAGENT_WORKFLOW_MAX_RUNS` 视范式）。

## Decision (ADR-lite)

**Context**：workflow MVP（06-06）刻意砍掉后台/面板/resume/budget 四扩展位，现补齐以对标 CC `Workflow`。
**Decision**：
- 后台 = opt-in `run_in_background` flag（默认同步、向后兼容），完成完整 summary 异步回灌（Q1=a+a）。
- registry 内存进程级 + 会话作用域，落盘跨重启列 Out of Scope（Q2=a）。
- resume = tool input `resume_from` 字段（LLM 驱动）+ 缓存键 `id+prompt-hash+DONE` + 重跑下游级联失效（Q3=a+a）。
- budget = `token_budget` tool 字段 + config 默认 + 共享 TokenTracker 透传 + 层间检查、层粒度（Q4=a+新管线+a）。
- 面板 = `/workflows` list/detail/clear 三件套，纯人看、不做 cancel（Q5）。
- registry FIFO 上限 `max_runs` 默认 50；会话切换 clear、在飞后台 workflow 续跑并通知（发散扫描全按推荐）。
**Consequences**：与 SubagentRegistry/Scheduler/后台子代理的内存级 + daemon + 通知范式严格一致，认知负担低；
不引入文件竞争/序列化；层粒度 budget 是软护栏（最后一层可能略超）；resume 限同会话（registry clear 后 fail-open）。

## Out of Scope

- 落盘跨重启 / 崩溃恢复（registry 仅内存进程级，后续独立单元）。
- 取消运行中的后台 workflow（`/workflows cancel`，与后台子代理不可中途取消一致）。
- 循环/条件/动态 fan-out（仍静态 DAG，分支靠主循环二次发 workflow）。
- 嵌套 workflow、worktree per-node 隔离、节点级 wall-clock 超时 / model 覆盖、schema 强制结构化输出。
- 节点级精确 budget 计费 / 下一节点开销预测（仅层间软护栏）；budget 的 1h 缓存精确计价；Retry-After。
- 给 LLM 暴露 workflow 查询工具（结果靠异步回灌）。

## Implementation Plan (small PRs)

- **PR1（纯逻辑 + 计量管线骨架）**：`core/workflow.py` 加 `compute_resume_plan` + `run_workflow` 的
  `prior_results`/`token_budget`/`on_node_status` 参数 + summary 标注；`run_subagent`→`agent_loop`
  token_tracker 透传。全单测覆盖。
- **PR2（registry + 后台执行 + drain）**：`WorkflowRegistry`（锁/FIFO/id）；`_install_workflow_handler`
  扩展三字段；`_drive_workflow_run` + `_drain_workflow_results`；会话切换 clear 接线。
- **PR3（面板 + 配置 + 文档 + 边界）**：`/workflows` 命令；`WorkflowConfig` 新字段 + 解析 + env；
  CLAUDE.md / config.toml 文档；边界测试（resume fail-open、budget 耗尽、session 切换在飞）。

## Technical Notes

- 关键文件：`src/core/workflow.py`、`src/core/handlers/workflow.py`、`src/main.py`
  （`_run_node_batch`/`_install_workflow_handler`/`WorkflowConfig`/`_parse_workflow_config`/
  5 处 install 点 + REPL 命令登记 + 会话切换 clear 点）、`src/concurrency/{background,notification}.py`、
  `src/memory/token_tracker.py`、`src/planning/subagent.py`（token_tracker 透传）。
- 线程安全参考：`src/concurrency/scheduler.py`（后台不碰 console）；范式参考：`SubagentRegistry`
  （FIFO/clear 生命周期）、`_drain_team_mailbox` + `pending_team_messages`（异步回灌去重）、
  `/loop` list/cancel/clear + `/reload`（命令登记三件套）。
- 现状基线：`workflow` 同步阻塞、`on_progress=console.print_status` 主线程、节点 fail-closed
  `permission.clone(fail_closed=True)` + `run_subagent`、`inject_notifications` 截断 500 字。
