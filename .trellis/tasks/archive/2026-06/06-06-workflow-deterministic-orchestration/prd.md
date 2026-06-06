# Workflow Deterministic Orchestration

## Goal

给 BareAgent 补上一个**确定性多智能体编排层**，对标 Claude Code 的 `Workflow` 工具：用代码/规格描述的、确定性的控制流（并行 fan-out、阶段、依赖、结果聚合）来批量调度子代理，而不是靠 LLM 在主循环里一次次手动发 `subagent` 工具调用（那是模型驱动、非确定性、且天然串行）。核心价值 = **并行**（一次跑 N 个子代理）+ **确定性结构**（执行顺序由编排描述固定）+ **结果聚合**（上游产物喂给下游 / 汇总给调用方）。

## What I already know

现有基础设施（Explore 勘察，含 file:line）：

- `src/planning/subagent.py:run_subagent(...)` 是子代理执行入口：参数含 `agent_type` / `run_in_background` / `isolation("none"|"worktree")` / `max_depth` / `current_depth` / `retry_policy` / `permission`；同步路径走 `_run_subagent_sync()` 跑一个隔离 `agent_loop`，返回末次响应文本；后台路径生成唯一 `task_id` 交 `BackgroundManager.submit` 即返回。
- `src/planning/agent_types.py`：4 个内置类型（general-purpose / explore / plan / code-review），`filter_tools`/`filter_handlers` 按类型过滤，`MAIN_LOOP_ONLY_TOOLS` 恒剥离。
- `src/concurrency/background.py:BackgroundManager`：`submit(task_id, fn, *args)`（task_id 去重，daemon 线程）/ `drain_notifications()`；结果只经 notification 队列浮现，**无直接返回值收集**。
- `src/concurrency/scheduler.py:Scheduler`（`/loop`）：纯定时 + 重排，`_fire` 用唯一 `run_id` 绕过 submit 去重，整体 try 包裹（线程异常不外逃），`threading.Lock` 保护字典——**多智能体并发的线程安全范式参考**。
- `src/team/`：`MessageBus`(JSONL 邮箱) / `ProtocolFSM`(PLAN_APPROVAL、SHUTDOWN 请求-响应) / `AutonomousAgent`(空闲-轮询-认领守护循环) / `TeammateManager`——有协作原语，但**无通用 fan-out / 并行收集**。
- `src/core/loop.py:agent_loop(...)` 是执行原语，可被编排层复用（`skill_gen=None` 隔离反思）。
- REPL slash 命令范式（`/goal`、`/loop`）：同步阻塞、非持久化、纯逻辑模块 + 注入驱动回调可单测——`src/core/goal.py` / `src/core/retry.py` / `src/planning/skill_gen.py` 是直接可仿的形态。
- ROADMAP / 代码中**无任何现存 workflow / DAG / pipeline 原语**——这是全新一层。

## Assumptions (temporary)

- 仿 `/goal`：纯逻辑编排引擎（拓扑/就绪集/结果穿线可单测）+ main.py 注入 `run_subagent` 执行器 + 线程池并行层。
- 复用 `run_subagent(agent_type=...)` 作节点执行器；并行层用 `ThreadPoolExecutor`（或 `BackgroundManager`）+ 并发上限。
- 同步阻塞 REPL（像 `/goal`），非持久化，Esc/中断可终止。

## Decision (ADR-lite) — Q1 编排形态与作者

**Context**: 需在"接近 Claude Code 程度 / 实现重量 / 安全性"间取舍编排描述的形态与作者。
**Decision**: **方案 B —— LLM 经一个隔离的 `workflow` 工具临场产出受 schema 约束的声明式 DAG（节点 + 依赖 + 阶段），纯引擎拓扑跑 + 并发 + 结果穿线。** 不走可执行 Python 脚本运行时（C）、不走用户手写文件（A）。
**Consequences**:
- 拿到 CC 核心价值（LLM 临场编排 + 并行 + 确定性结构），完全避开任意代码 `exec` 的沙箱/安全/可测性难题与 resume/budget 的半成品风险。
- 代价：表达力受声明式限制——无循环 / 条件 / 动态 fan-out（静态 DAG）。这些划入 Out of Scope（LLM 仍可主循环里多次发 `workflow` 补足）。
- `workflow` 工具走 `skill_create`/`goal_verdict` 隔离三件套：不进全局集、只在主循环注入、子代理拿不到（MVP 无嵌套 workflow）。

## Decision (ADR-lite) — Q2 执行模式

**Decision**: **同步阻塞工具调用**——`workflow` handler fan-out 子代理（线程池 + 并发上限）→ 等全图跑完 → 聚合文本作 tool_result 回灌 LLM；`on_progress` 回调往 console 打 phase/节点进度行，并发线程绝不碰 messages/console。后台 run-id + notification + 进度面板 + resume 整块留作后续扩展位。

## Decision (ADR-lite) — Q3 失败语义

**Decision**: **跳过下游、独立分支继续**。失败节点（执行器抛异常）把其传递性下游依赖转 `skipped`；无依赖关系的并行分支照常跑完；收尾结构化回报每节点 `status ∈ done/failed/skipped` + 产物/错误摘要。纯引擎里在就绪集计算时实现："任一上游 failed/skipped ⇒ 本节点转 skipped"。

## Resolved defaults（按既有范式直接定，非阻塞）

- **节点执行器**：复用 `run_subagent(provider, task, tools, handlers, permission, agent_type=..., retry_policy=...)` 同步路径，不另起子代理执行栈。
- **并发层**：`ThreadPoolExecutor`（仿 `Scheduler` 线程安全范式，daemon、Lock 保护共享状态、worker 绝不碰 messages/console）；并发上限 = 配置 `max_concurrency`，默认 `min(8, (os.cpu_count() or 4))`（保守，子代理本身可能再 spawn）。
- **结果穿线**：上游节点产物（子代理返回文本）注入下游节点 prompt——`{{<node_id>}}` 占位替换 + 无占位时按 `depends_on` 顺序自动附带"上游产物"段。
- **权限/隔离**：节点子代理沿用 `run_subagent` 的 `for_subagent` fail-closed 隔离，**绝不自动升级**（与 /goal、Plan 审批、/loop、hooks 一脉相承）；`workflow` 工具不进全局集、只主循环注入、子代理拿不到（无嵌套 workflow）。
- **配置**：`[workflow]` 段（`enabled` 默认 ON + env `BAREAGENT_WORKFLOW_ENABLED`、`max_concurrency`、`max_nodes` 防爆、`default_agent_type`），`_parse_workflow_config` 逐字段容错不崩 boot，restart-required（不进热重载 hot 集）。
- **DAG schema**：`nodes: [{id, prompt, agent_type?, depends_on?: [id], phase?, label?}]`；引擎校验 id 唯一、depends_on 引用存在、无环（环 ⇒ 结构化 Error 不执行）。

## Decision (ADR-lite) — Q4 MVP 边界

**Decision**: 按推荐的最小可用 MVP（静态声明式 DAG + 并行执行器 + 失败语义(b) + 结构化聚合），下列全部留作后续扩展位：动态 fan-out / 循环 / 条件；可执行脚本运行时；后台执行 + `/workflows` 面板 + resume + token budget；worktree per-node 隔离（节点默认 `isolation="none"`）；嵌套 workflow；节点 wall-clock 超时；节点级 model 覆盖；schema 强制结构化输出。

## Requirements (final)

- **确定性**：给定相同 DAG 描述，执行结构固定（节点集 + 依赖边 + 阶段顺序不由 LLM 临场翻转）。
- **LLM 临场编排**：LLM 经隔离 `workflow` 工具产出受 schema 约束的 `nodes` DAG。
- **并行**：相互独立、依赖已就绪的节点并发执行，受 `max_concurrency` 约束。
- **依赖 + 阶段**：`depends_on` 定义偏序；`phase` 仅用于进度分组展示。
- **失败语义 (b)**：节点执行抛错 ⇒ failed；其传递性下游 ⇒ skipped；独立分支继续。
- **结果穿线**：上游产物注入下游 prompt（`{{node_id}}` 占位替换 + 无占位时按 depends_on 顺序自动附带）。
- **结构化聚合**：收尾把每节点 `status ∈ done/failed/skipped` + 产物/错误摘要拼成 tool_result 回灌 LLM。
- **复用既有栈**：节点执行器 = `run_subagent`（同步路径，agent_type/retry_policy 透传），不另起子代理执行栈。
- **线程安全**：纯引擎调度逻辑可单测（无线程）；并发由注入的 `map_concurrent` 提供；**节点子代理 console=None 静默运行，所有 console 输出仅由 driver 在主线程发**（仿 Scheduler 不在 timer 线程碰 console）。
- **权限 fail-closed**：节点子代理走 `for_subagent` 隔离，绝不自动升级；`workflow` 工具不进全局集、子代理拿不到（无嵌套）。
- **防爆**：`max_nodes` 上限 + 环检测 ⇒ 结构化 Error 不执行；Esc/中断取消在飞节点并干净收尾。

## Acceptance Criteria (final)

- [ ] 能让 LLM 产出并执行一个多节点、含并行分支 + 依赖的 DAG，得到结构化聚合结果回灌推理链。
- [ ] 纯引擎单测覆盖：校验（id 唯一 / depends_on 引用存在 / 环检测 / max_nodes）、就绪集计算、失败语义(b) 下游 skip 传播、结果穿线（占位 + 自动附带）、聚合格式。
- [ ] 并发：独立节点经注入 map_concurrent 并发；driver 进度回调仅主线程；节点静默。
- [ ] `workflow` 工具不在 `get_tools()` 全局集；`filter_tools` 对所有子代理类型恒剥离（MAIN_LOOP_ONLY_TOOLS）。
- [ ] 权限 fail-closed 不升级；非交互/中断语义明确。
- [ ] `_parse_workflow_config` 逐字段容错不崩 boot；`enabled=false` 全链路短路（工具不注入）。
- [ ] CLAUDE.md 增节 + config.toml `[workflow]` 注释齐备；ruff 干净（仅 format 改动文件）；全量 pytest 绿。

## Definition of Done (team quality bar)

- 纯逻辑核心单测齐全（拓扑/就绪/穿线/失败/环/并发上限）
- ruff check 干净（只 format 改动文件）；全量 pytest 绿
- CLAUDE.md 增节 + config.toml 注释（若引入 [workflow] 段）
- 权限/隔离/失败语义在 PRD 与代码注释中一致

## Out of Scope (确定，后续扩展位)

- 可执行 Python 脚本运行时 / 任意代码 `exec`（Q1 砍掉，声明式 DAG 替代）
- 动态 fan-out / 循环 / 条件（loop-until-budget、pipeline-over-runtime-list）—— 静态 DAG
- 后台执行 + `/workflows` 实时面板 + 跨会话 resume / journaling / token budget
- 用户手写 workflow 文件 + `/workflow run <file>`（Q1 选 LLM 临场作者）
- worktree per-node 隔离（节点默认 `isolation="none"`，真要并行写盘留后续）
- 嵌套 workflow（节点里再起 workflow）
- 节点 wall-clock 超时、节点级 model 覆盖、schema 强制结构化输出（自由文本产物 + 主循环二次编排足够）

## Technical Approach

**模块布局（仿 `goal.py` / `retry.py` 纯逻辑 + 注入驱动范式）：**

- `src/core/workflow.py`（NEW，纯逻辑，零 LLM/loop/线程/SDK 依赖，可单测）：
  - `WorkflowNode`(id, prompt, agent_type|None, depends_on:list[str], phase|None, label|None)、`NodeStatus`(PENDING/RUNNING/DONE/FAILED/SKIPPED)、`NodeResult`(id, status, output, error)、`WorkflowError`
  - `parse_workflow(tool_input)` 容错解析 → `WorkflowSpec`
  - `validate_workflow(spec, *, max_nodes)` → 错误列表（id 唯一 / depends_on 引用存在 / 无环 / ≤max_nodes）
  - `ready_nodes(spec, statuses)`（deps 全 DONE 且自身 PENDING）+ `propagate_skips(spec, statuses)`（任一上游 failed/skipped ⇒ 转 skipped）
  - `build_node_prompt(node, results)`（`{{id}}` 占位替换 + 无占位按 depends_on 自动附带上游产物段）
  - `format_summary(spec, results)`（结构化聚合文本）
  - `run_workflow(spec, *, execute_node, map_concurrent, on_progress)` 驱动器：循环 compute ready-set → `map_concurrent` 并发跑该批 → 更新 statuses + propagate_skips → 直到全部 terminal；`execute_node`/`map_concurrent`/`on_progress` 全注入（测试注同步 map + 假执行器；main 注线程池 + run_subagent）
- `src/core/handlers/workflow.py`（NEW）：`WORKFLOW_TOOL_SCHEMA`（`nodes` 数组）+ 薄 handler（解析→校验→驱动→格式化）；**不进** `get_tools()` 全局集（同 `goal_verdict`/`skill_create`/`exit_plan_mode`）。
- `src/main.py`（MODIFIED）：`WorkflowConfig` + `_parse_workflow_config`（env `BAREAGENT_WORKFLOW_ENABLED`）+ 构造 workflow handler 闭包（捕获 provider/tools/handlers/permission/retry_policy；节点 console=None 静默；`map_concurrent` = `ThreadPoolExecutor(max_workers=max_concurrency)` 包装，仿 Scheduler 线程安全；`on_progress`→主线程 console）+ `_install_workflow_handler`（仿 `_install_plan_handler`，boot + 每次会话切换重装）+ `enabled=false` 不注入。
- `src/planning/agent_types.py`（MODIFIED）：`"workflow"` 入 `MAIN_LOOP_ONLY_TOOLS`（所有子代理类型恒剥离）。
- `src/permission/guard.py`：`workflow` 权限档对齐现有 `subagent` 工具（编排本身不引入新副作用面，真正写盘由节点子代理 `for_subagent` fail-closed 逐个 gate）——实现时核对 `subagent` 的 SAFE_TOOLS 归属后一致处理。
- `config.toml` `[workflow]` 段 + `CLAUDE.md` 增节 + `tests/test_workflow.py`（纯引擎单测）。

**关键不变量**：(1) 纯引擎不碰线程/console/LLM；(2) 节点子代理静默、driver 独占主线程 console；(3) 工具三重隔离（不进全局集 + MAIN_LOOP_ONLY_TOOLS + filter_handlers 丢孤儿）；(4) `enabled=false` 字节级短路。

## Technical Notes

- 仿形态：`src/core/goal.py`（纯逻辑 + 注入驱动）、`src/concurrency/scheduler.py`（线程安全并发）、`src/planning/subagent.py`（节点执行器）。
- 隔离工具范式：若用 LLM 工具，考虑 `skill_create`/`goal_verdict` 的"不进全局集、只在特定上下文暴露、子代理拿不到"三件套。
- 研究参考：Claude Code `Workflow` 工具语义（agent/parallel/pipeline/phase/log、并发上限 min(16, cores-2)、schema 结构化输出、worktree 隔离、resume 日志）——MVP 取其确定性 fan-out 内核，砍掉脚本运行时与 resume。
