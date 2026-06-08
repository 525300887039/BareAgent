# team 收口：team_register 动态建队友 + PLAN_APPROVAL 发送侧接线

## Goal

补齐 team 子系统两个明确遗留的半成品（06-06-team-subsystem-completion 的 Out of Scope，现纳入）：
1. **`team_register`**：LLM 能动态注册队友定义，而非只能手改 `.team.json`。
2. **PLAN_APPROVAL 发送侧**：接收侧已存在（队友能审阅并回裁决），但没有发送侧——主代理发不出审批请求。补一个工具让 LLM 把计划/提案发给队友审批、阻塞拿回裁决。

队友现在已能记忆（06-08-team-stateful-memory），收口这两块后 team 从"可用协作闭环"升到"LLM 全自助创建 + 审批闭环"。

## What I already know

- `TeammateManager.register(name, role, system_prompt, provider_config=None)`（`src/team/manager.py`）**已存在**：校验三者非空否则 ValueError，落 `.team.json`（RLock 线程安全）。`team_register` 工具只需把它接成 client tool。
- `ProtocolFSM.request(to, Protocol.PLAN_APPROVAL, content)`（`src/team/protocols.py`）编码协议进消息 → 队友 `_build_incoming_prompt` 识别 PLAN_APPROVAL 包成"请审阅计划"prompt → `respond` 同协议回 → 发送方 `wait_response` + `decode_protocol_content` 取裁决 body。**接收侧全通，只差发送侧调用点**。
- 现有 `_team_send`（main.py:1767）已是**阻塞式**：not-running / main-target 立即返回、`wait_response(timeout)`、`mark_delivered` 去重——PLAN_APPROVAL 发送侧可复用这套骨架，差别仅在用 `ProtocolFSM.request(...PLAN_APPROVAL...)` 而非裸 `msg_type=request` plain content。
- team 工具五处接线（照 team_spawn/team_send 镜像）：(1) `core/tools.py:TEAM_TOOL_SCHEMAS`（`_schema = tool_schema`）；(2) `DEFERRED_TOOLS` set；(3) `_TEAM_FALLBACK_HANDLERS`（tools.py ~485，经 `handlers.update(team_handlers or _TEAM_FALLBACK_HANDLERS)` 合并，tools.py:604）；(4) `_make_team_handlers` 返回 dict（main.py:1734-1883）；(5) `/team` slash 子命令 `_handle_team_command`（main.py:1933）+ usage 文案。
- 队友 provider override 经 `_make_teammate_provider_factory`：读 `provider_overrides` dict 的 `name`/`model`/`api_key_env`/`base_url`/`wire_api`，未给则继承会话 provider（同名时连 api_key_env/base_url/wire_api 一并继承）。
- 权限：现有 team 工具均**非 SAFE**（team_send/team_spawn/team_shutdown），DEFAULT 下确认。

## Decision (ADR-lite，全部已定)

- **Q1 → (a) 扁平串**：`team_register(name, role, system_prompt, provider?, model?)`。`provider`/`model` 可选，省略继承会话 provider；内部组 `provider_config={name,model}` 丢空值。罕见 override（api_key_env/base_url/wire_api）不进 MVP 工具（手改 .team.json）。never-raise（空必填字段返回结构化 Error）。
- **Q2 → (a) 独立工具 `team_request_review(to_agent, plan)`**：复刻 `_team_send` 阻塞骨架（not-running/main 立即返回、`wait_response(timeout)`、`mark_delivered` 去重），内部走 `ProtocolFSM.request(to, Protocol.PLAN_APPROVAL, plan)`，`decode_protocol_content` 取裁决 body 回灌 LLM。
- **Q3 → scope 定**：register **不自动 spawn**（两步）；审批工具**不接管** exit_plan_mode（独立 LLM 可调工具）；两工具都加 `/team` slash 子命令（`/team register` / `/team review`）+ usage 更新；权限都**非 SAFE**（对齐现有 team 工具）。

## Requirements (evolving)

- `team_register` 工具：注册队友定义到 `.team.json`，never-raise（空字段返回结构化 Error）。
- PLAN_APPROVAL 发送侧工具：把计划发给运行中的队友、阻塞拿回审批裁决回灌 LLM；队友未运行 / 目标为 main 立即返回说明；超时返回提示。
- 两工具走完整五处接线 + 非 SAFE 权限档。
- 新增行为有 pytest 覆盖。

## Acceptance Criteria (evolving)

- [ ] `team_register(name, role, system_prompt, ...)` → `.team.json` 出现该队友，`team_list` 可见；空字段返回 Error 不抛。
- [ ] 注册的队友可被 `team_spawn` 拉起。
- [ ] PLAN_APPROVAL 发送侧 → 运行中队友收到的是 PLAN_APPROVAL 协议消息（`_build_incoming_prompt` 走审批分支），裁决经 decode 返回；not-running/main 立即返回；超时有提示。
- [ ] 两工具在 DEFERRED_TOOLS / fallback / handlers / slash 五处齐备；非 SAFE。
- [ ] ruff 干净，全量测试绿。

## Definition of Done

- Tests added；Lint / 全量测试 green；CLAUDE.md team 小节补注；config 无新增（沿用 [team]）。

## Out of Scope (explicit)

- 队友记忆/续跑（已由 06-08-team-stateful-memory 覆盖）。
- exit_plan_mode 与 PLAN_APPROVAL 的自动联动（审批工具是独立 LLM 可调工具，不自动接管 plan 模式）。
- team_register 自动 spawn（register/spawn 保持两步，对齐 TeammateManager 现有拆分）。
- 注销/编辑队友（team_unregister/team_update）、邮箱 O(n²) 轮转、多人并行审批聚合。

## Technical Notes

- 关键文件：`src/core/tools.py`（2 个 schema + DEFERRED + fallback）、`src/main.py`（`_make_team_handlers` 加 2 handler + `_handle_team_command` slash + usage）、复用 `src/team/manager.py`（register）、`src/team/protocols.py`（PLAN_APPROVAL，不改）。
- 复用范式：PLAN_APPROVAL 发送侧复刻 `_team_send` 阻塞骨架（not-running 预判 / wait_response / mark_delivered 去重）。
