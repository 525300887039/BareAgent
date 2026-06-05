# plan-mode-workflow

## Goal

把 PLAN 模式从"只读权限闸"升级为真正的 **plan → 呈递 → 审批 → execute** 工作流,对标 Claude Code 的 `ExitPlanMode` 体验:在 PLAN 模式下 agent 用只读工具调研、产出一份实现计划,通过一个 `exit_plan_mode` 工具把计划呈递给用户,用户一键批准后自动切到执行模式并在同一对话里继续落地;拒绝则留在 PLAN 带反馈改稿。

## What I already know

- 现 PLAN 实现 = 纯权限闸(`permission/guard.py`):PLAN 下 `requires_confirm` 对非 `SAFE_TOOLS` 一律 True,`ask_user` 对 PLAN 直接 `print("Plan mode: X blocked (read-only)")` 返回 False。
- LLM 在 PLAN 下**无任何引导**,只会盲目撞写工具被拒——缺"该研究+产计划+呈递"的指令。
- system prompt 会话内静态(`messages[0]` via `assemble_system_prompt`),但有现成"每轮刷新注入"范式:`_refresh_nag_reminder` / `_refresh_memory_recall` 在 `compact_fn`(`_build_loop_compact`)里每轮 agent_loop 调用时改写 messages。计划模式指令可走同款注入,按 `permission.mode` 开关。
- 工具/handlers 会话级建一次(`tools`/`handlers`)各 turn 共享;`permission.mode` 是可变属性,`/plan`、`/mode`、Shift+Tab(`_cycle_permission_mode`)原地切。
- 已有 `ask_user_fn` 确认回调(`main.py:1136` 注入 `permission._ask_user_fn = _ask`),计划审批交互可复用同款 prompt-toolkit 机制。
- 新工具注册范式:schema 在 `core/schema.py`,handler 在 `core/handlers/`,经 `core/tools.py` 的 `BASE_TOOLS`/`DEFERRED_TOOLS` 注册;`skill_create` 已示范"只在特定上下文暴露、入 SAFE_TOOLS 不弹确认、子代理拿不到"的范式。
- 子代理隔离:`skill_create`/`hook_engine` 等主循环专属能力不传子代理,`exit_plan_mode` 同理应排除。

## Assumptions (temporary)

- 进入 PLAN 的入口(`/plan` / Shift+Tab / `/mode`)不变,本任务只补"在 PLAN 里干什么 + 怎么退出 PLAN"。
- 审批交互在 agent_loop 内同步发生(复用 ask_user 通道),批准后翻转 `permission.mode` 并让同一 loop 续跑实现,不打断对话。

## Open Questions

(全部已解决)

## Resolved Decisions

- (Q1) 退出审批机制 = **方案 A:in-loop 工具同步审批**。`exit_plan_mode(plan=...)` handler 当场经 `ask_user` 通道弹审批 → 批准则原地翻转 `permission.mode` + 工具结果回灌"已批准,开始实现" → 同一 agent_loop 不中断续跑;拒绝则反馈回灌、留 PLAN。复用已有 `_ask_user_fn` + 可变 mode + in-loop 结果回灌,接线最少、体验最贴 Claude Code。
- (Q2) 批准后模式 = **三选一审批**(对标 Claude Code Yes/Yes-auto/No):1) 批准→`DEFAULT`(写仍逐个确认) 2) 批准并自动执行→`AUTO`(危险命令仍拦) 3) 拒绝→留 `PLAN`。审批回调返回枚举 approve-default / approve-auto / reject。
- (Q3) 拒绝路径 = **收集可选理由**。拒绝后追问"拒绝理由(可空)",非空则回灌 LLM 作改稿依据,空则回灌通用"计划被拒,请修订";留 PLAN。
- (Q4) 计划持久化 = **不落盘,仅留对话**。计划作为 `exit_plan_mode` 工具结果留在 messages,由已有 `/export` + trellis `prd.md` 承载持久化;自造计划存储是重复建设,落盘进 Out of Scope。
- (Q5) 计划指令注入 = **MVP 必含**。按 `permission.mode == PLAN` 每轮(每迭代)经 compact 注入 `<plan-mode>` system 段(仿 `_refresh_nag_reminder`:先剥旧块再按条件注入),引导 LLM 只读调研→调 `exit_plan_mode`;非 PLAN 不注入。因 `compact(messages)` 在**每个 LLM 迭代开头**调用(`loop.py:53`),模式翻转后下一迭代自动剥离指令,无残留。

## Requirements

- **R1 计划指令注入**:`permission.mode == PLAN` 时,每个 agent_loop 迭代经 compact 注入 `<plan-mode>` system 段(只读调研、不改文件、完成调 `exit_plan_mode` 呈递计划),先剥旧块再条件注入;非 PLAN 不注入。
- **R2 `exit_plan_mode` 工具**:新增 client tool,入参 `plan`(markdown,必填);入 `SAFE_TOOLS`(PLAN 下不被拦);子代理不可见(排除清单)。
- **R3 审批交互**:handler 经 AgentConsole 渲染计划 → 经审批回调收三选一(approve-default / approve-auto / reject);拒绝追问可选理由。
- **R4 批准转执行**:approve-default→翻 `permission.mode=DEFAULT`、approve-auto→`AUTO`;工具结果回灌"计划已批准,开始实现",**同一 agent_loop 不中断续跑**;下一迭代 compact 自动剥离计划指令。
- **R5 拒绝改稿**:reject 留 PLAN,把"用户拒绝。理由:…"(空则通用语)作工具结果回灌 LLM。
- **R6 失败/边界 fail-closed**(见 Edge Cases):非交互/fail-closed guard/中断 → 视为 reject 不翻转;非 PLAN 调用 → no-op 错误;空 plan → 结构化报错。
- **R7 文档**:CLAUDE.md 增补 PLAN 工作流段。

## Acceptance Criteria

- [x] AC1:PLAN 下注入 `<plan-mode>` 指令引导只读调研 + 调 `exit_plan_mode`(`_refresh_plan_directive` + `PLAN_MODE_DIRECTIVE`;单测覆盖注入/剥离)。
- [x] AC2:`exit_plan_mode` handler 触发三选一审批(`_make_plan_approval`;单测覆盖 1/2/3 分支可达)。
- [x] AC3:approve-default 后 `mode==DEFAULT`、approve-auto 后 `mode==AUTO`(单测断言翻转);同 loop 续跑由 in-loop 工具结果回灌 + per-iteration compact 自动剥离指令保证。
- [x] AC4:reject 后 `mode` 仍 PLAN,LLM 收到拒绝理由(含空理由通用回灌)——单测覆盖。
- [x] AC5:`filter_tools` 对全部 4 个内置子代理类型(含 general-purpose)剥离 `exit_plan_mode`——单测遍历断言。
- [x] AC6:非 PLAN 零回归——`permission!=PLAN` 不注入指令(单测)、非 PLAN 调用 handler→noop Error(单测);`tools` 保持 base 喂所有 handler 构建。
- [x] AC7:边界 fail-closed——非交互/`fail_closed`→unavailable 不翻转、空 plan→Error、EOF/中断→reject(单测各覆盖)。
- [x] AC8:`tests/test_plan_mode.py` 28 项,覆盖 SAFE_TOOLS + PLAN requires_confirm 放行、handler 三分支+边界、注入刷新+幂等+翻转剥离、`_build_loop_compact` 接线、子代理隔离、`_install_plan_handler` 重装契约。

## Status: DONE

实现完成,全量 932 passed / 3 skipped(本机 localhost flaky) / 47 deselected(manual),ruff check + format 全过。改动文件:`src/core/handlers/plan.py`(新)、`src/core/context.py`、`src/main.py`、`src/permission/guard.py`、`src/planning/agent_types.py`、`tests/test_plan_mode.py`(新)、`CLAUDE.md`。未提交(等用户确认)。

## Edge Cases (expansion sweep)

- **非交互 / 无 TTY / fail_closed guard**:审批无法取得 → 等同 reject,返回"无可用交互审批,留在 PLAN",**不翻转模式**(对齐 `ask_user` 现有 non-tty fail-closed 行为)。
- **非 PLAN 模式下被调用**:防御性 no-op,返回"当前不在 PLAN 模式,无需退出",不动 mode。
- **空 / 缺失 plan 入参**:结构化 tool error "plan is required",不触发审批。
- **审批中 KeyboardInterrupt / EOF**:捕获 → 视为 reject,留 PLAN,不崩。
- **模式翻转后同 loop 续跑**:批准后该 loop 后续工具走新 mode;`<plan-mode>` 指令由下一迭代 compact 自动剥离(per-iteration cadence,`loop.py:53`)。
- **子代理边界**:plan/explore 只读子代理绝不能拿 `exit_plan_mode`(否则可翻父级 mode);经 `disallowed_tools`/工具集排除,与 `skill_create`/`semantic_rename` 同档。

## Technical Approach

- **指令注入**:`main.py` 加 `_refresh_plan_directive(messages, permission)`,在 `_build_loop_compact` 的 `_compact` 里调用(与 `_refresh_nag_reminder`/`_refresh_memory_recall` 并列);常量 `_PLAN_DIRECTIVE_PREFIX = "<plan-mode>"`,剥旧块逻辑复刻 nag。指令文本可定义在 `core/context.py` 作可测常量。
- **工具 schema**:`core/schema.py` 加 `EXIT_PLAN_MODE_TOOL_SCHEMA`(`{plan: string(required)}`)。
- **handler**:`core/handlers/plan.py:run_exit_plan_mode`,闭包注入 `permission` 引用 + 审批回调 `approve_fn(plan)->{"approve-default"|"approve-auto"|"reject", reason}`;据结果翻 mode 或回灌拒绝。读 `permission.mode` 守卫非 PLAN no-op、fail_closed→reject。
- **注册**:`core/tools.py` 把 `exit_plan_mode` 注入工具集 + 入 `PermissionGuard.SAFE_TOOLS`;`planning/agent_types.py` 的 `_READ_ONLY_DEFAULTS["disallowed_tools"]` 加入(子代理排除),`subagent.py` 主循环专属不透传(同 skill_create/hook_engine)。
- **审批回调接线**:`main.py` 建 handler 时注入一个基于 prompt-toolkit / AgentConsole 的三选一审批 UI(复用 `_ask_user_fn` 同款机制),返回枚举 + 可选理由。
- **模式翻转**:handler 直接改 `permission.mode`(可变属性,与 `/plan`、`_cycle_permission_mode` 同款原地改)。

## Decision (ADR-lite)

**Context**:PLAN 现为纯只读权限闸,LLM 无引导、无法"产出计划→批准→执行",体验与 Claude Code 差距明显。
**Decision**:in-loop `exit_plan_mode` 工具(方案 A)+ 三选一审批(default/auto/reject)+ 拒绝收集理由 + 计划不落盘 + 按 mode 的 compact 指令注入。全程复用既有 `_ask_user_fn`、可变 `permission.mode`、`_refresh_nag_reminder` 注入范式、in-loop 工具结果回灌四件套,新增面最小。
**Consequences**:体验贴 Claude Code、接线最少、非 PLAN 零回归;代价是 handler 需闭包持有 permission + 审批回调(已有 skill_create/hook_engine 先例)。落盘/计划历史/子代理自动 ExitPlanMode 留作后续。

## Definition of Done (team quality bar)

- Tests added/updated (unit where appropriate)
- ruff check / pytest green
- CLAUDE.md 增补 PLAN 工作流段(若行为变化)
- 非 PLAN 路径零回归

## Out of Scope (explicit)

- 计划落盘持久化 / 多计划历史(已有 `/export` + trellis `prd.md` 承载;后续扩展位)
- 计划模式下的子代理自动 ExitPlanMode
- 富 TUI 计划编辑器(只读渲染 + 批准/拒绝即可,不做行内编辑计划)
- PLAN 入口改造(`/plan`、Shift+Tab、`/mode` 不动)
- 配置化(审批默认目标模式、指令文本暂硬编码,不加 `[plan]` 配置段)

## Implementation Plan (small PRs)

- **PR1 — 工具骨架 + 权限放行 + 子代理排除 + 测试**:`EXIT_PLAN_MODE_TOOL_SCHEMA`、`run_exit_plan_mode`(含非 PLAN no-op / 空 plan 报错 / fail_closed→reject 边界)、注入 SAFE_TOOLS、子代理 disallowed,纯逻辑单测(注入 fake approve_fn/permission)。
- **PR2 — 指令注入 + 审批接线 + 模式翻转**:`_refresh_plan_directive` 接入 compact;`main.py` 审批回调三选一 UI + 理由收集;handler 翻 `permission.mode` + 结果回灌;注入刷新/翻转单测。
- **PR3 — 收尾**:CLAUDE.md 增补 PLAN 工作流段,边界回归测试补齐,ruff/pytest green。

## Technical Notes

- 对标基准:Claude Code `ExitPlanMode`(本 agent 第一手认知,无需 research 子代理)。
- 关键文件预判:`src/permission/guard.py`(SAFE_TOOLS + ask 流)、`src/core/schema.py`(工具 schema)、`src/core/handlers/`(新 handler)、`src/core/tools.py`(注册 + 子代理排除)、`src/core/context.py` 或 `src/main.py`(计划指令注入,仿 `_refresh_nag_reminder`)、`src/main.py`(审批回调 + 模式翻转接线)、`src/planning/subagent.py` / `agent_types.py`(子代理排除)。
