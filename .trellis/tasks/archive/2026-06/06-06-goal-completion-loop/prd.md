# goal 完成条件循环（/goal）

## Goal

给 BareAgent 增加对标 Claude Code `/goal` 的**完成条件驱动循环**：用户用 `/goal <可度量的完成条件>` 设定目标，agent 自动一轮接一轮工作，每轮 `agent_loop` 停止后由一个**独立评估器**判定条件是否满足；不满足就把评估理由回灌、继续下一轮；满足或触达 `max_turns` 边界则停止并清除目标。把 BareAgent 从"一问一答 + 手动续跑"补上"自驱动到达标"这一档自主能力。

## Requirements

- **命令面（同步自驱动模型，不持久化 → 无跨输入状态）**：
  - `/goal <condition>` —— 设定目标并**立即同步启动自驱动循环**，跑到达标/触界/中断才返回 REPL。
  - `/goal --max-turns N <condition>` —— 行内覆盖本次循环的轮数上限。
  - `/goal`（无参）—— 打印用法（无持久目标可查）。
  - 不做 `/goal clear`、`/goal` 状态、"已活跃则拒绝"——循环同步阻塞期间 REPL 不接受新输入，循环结束即无残留状态，这些子命令在本模型下无意义。Esc 中断正在跑的循环。
- **循环驱动**：注入初始指令启动 → `agent_loop` 跑到 assistant 无工具调用即停 → 评估器裁决 → 未达标把 `reason` 回灌为新 user 消息继续 → 达标 / 触界停止并清目标。挂载点 = `main.py:3002-3048` 用户 turn 块。
- **评估器（transcript-only）**：在**消息副本**上跑一次隔离调用（复刻 `_run_skill_reflection` 范式），强制调唯一工具 `goal_verdict(met: bool, reason: str)` 回结构化裁决；never-raise。
- **评估器模型**：`[goal] evaluator_model` 可配，**默认留空 = 回退复用会话 provider**；设了则经 `factory` 单独构造便宜 provider（对标 Claude Code 默认 Haiku）。
- **边界**：`[goal] max_turns` 默认 25（可配，逐字段容错）；支持 `--max-turns N` 行内覆盖；wall-clock 超时不做。
- **权限**：尊重当前权限模式、**绝不自动升级**；启动时若处 DEFAULT 模式打一行提示引导 `/auto`。
- **中断**：Esc / KeyboardInterrupt 可中断循环并清目标，回滚到一致状态。
- **纯逻辑可单测**：prompt 构造 + verdict 解析 + 循环驱动落 `src/core/goal.py`，注入假评估器 / 假 loop 可单测（仿 `retry.py` / `skill_gen.py`）。

## Acceptance Criteria

- [ ] `/goal <cond>` 能同步驱动多轮直到评估器判定达标后停止。
- [ ] 评估器未达标时把 `reason` 回灌为 user 消息，agent 下一轮能看到。
- [ ] 评估器首轮即判 `met=true` → 立即停、报已达成。
- [ ] 触达 `max_turns`（默认 25 或 `--max-turns N`）时安全停止并提示，不无限跑。
- [ ] `/goal`（无参）打印用法；空条件 / 非法 `--max-turns` 值报错。
- [ ] DEFAULT 模式下启动 `/goal` 打出 `/auto` 引导提示。
- [ ] Esc / `LLMCallError` 中断循环不破坏会话状态（复刻现有 except 回滚）。
- [ ] 评估器调用失败/未回 `goal_verdict` → 按"未达标"处理 + 警告，仍受 max_turns 约束。
- [ ] `[goal] evaluator_model` 留空时复用会话 provider；设值时经 factory 构造独立 provider。
- [ ] `src/core/goal.py` 纯逻辑有 pytest 覆盖（达标/未达标/触界/解析容错/边界行为）。
- [ ] `[goal]` 配置逐字段容错，坏配置不崩 boot。

## Definition of Done

- pytest 覆盖纯逻辑与边界；`ruff check src tests` 干净（只 `ruff format` 改动的文件，避免全树 churn）。
- `/goal` 进 `_SLASH_COMMANDS` + `_HELP_TEXT` + REPL dispatch if 链。
- `config.toml` 增 `[goal]` 段并注明安全语义（尊重权限、不自动升级）。
- CLAUDE.md 增补 `/goal` 架构段（与现有模块文档同风格、源码禁 emoji）。

## Technical Approach

- **新增纯模块 `src/core/goal.py`**（无 LLM/loop/REPL 依赖，注入可单测）：
  - `GoalState`（condition / max_turns / turns_used / active）。
  - `build_evaluator_prompt(condition, transcript_messages)` —— 构造评估器 user 消息。
  - `parse_verdict(...)` —— 从 `goal_verdict` 工具调用解析 `{met, reason}`，容错（缺字段/未调用 → 视为未达标 + 标记 malformed）。
  - 循环驱动器（注入 `run_turn` / `evaluate` 两个回调）：返回结构化终止原因（met / max_turns / aborted）。
- **评估器工具**：`goal_verdict(met, reason)` schema + handler，**刻意不进全局工具集**——只在评估器隔离调用里暴露（同 `skill_create` 范式），子代理天然拿不到。
- **REPL 接线（`main.py`）**：`_dispatch_goal_command` 解析子命令/flag；`_run_goal_evaluator`（仿 `_run_skill_reflection`，在 messages 副本上隔离调用）；循环复用现有用户 turn 块的 `agent_loop` 调用 + except 回滚；`GoalConfig` + `_parse_goal_config` + `_build_goal_provider`（evaluator_model 经 factory）。
- **配置范式**：`[goal]` → `GoalConfig` dataclass → `_parse_goal_config`（逐字段容错）→ build；`max_turns` 走 env `BAREAGENT_GOAL_MAX_TURNS` 覆盖，`evaluator_model` config-only。boot 固化，随 provider restart-required（不进热重载 hot 集）。
- **会话生命周期**：goal 同步跑完即返回、无残留状态，故 `/clear`、`/new` 无需额外清理；goal 内层逐轮**不触发** skill 反思（`skill_gen=None`），循环整体结束后照常收尾。中断/失败时回滚当前未完成 turn（`del messages[snapshot:]`），已完成轮次保留。

## Decision (ADR-lite)

- **Context**：ROADMAP 已收官，需从 Claude Code 新能力里挑高 ROI、低风险、与现有地基契合的来补；`/goal` 直接复用 `_run_skill_reflection` 的隔离评估范式。
- **Decision**：transcript-only 评估器（Q1）；evaluator_model 可配默认回退会话 provider（Q2）；max_turns 默认 25 + `--max-turns` 行内覆盖、不做超时（Q3）；尊重权限模式不自动升级 + DEFAULT 提示（Q4）；持久化/Stop-hook 通用化/只读验证工具/LLM 自设目标全列 Out of Scope（Q5）。
- **Consequences**：评估器可能被"我觉得做完了"糊弄（靠条件文案要求自证缓解，只读验证工具为后续）；per-turn 评估成本由 evaluator_model 旋钮控制；未达标循环靠 max_turns 兜底。

## Out of Scope (explicit)

- 跨 `/resume` 持久化未达标目标（goal 为会话运行时状态，退出即清）。
- 把 goal 抽象成通用 `Stop` hook 事件（不动现有 hooks 系统）。
- wall-clock 超时（轮数上限已是确定性安全闸）。
- 评估器只读验证工具（transcript-only 为 MVP）。
- 把 goal 暴露成 LLM 可调工具 / agent 自设目标（仅用户侧 `/goal`，防失控自驱动）。

## Technical Notes

- 挂载点：`main.py:3002-3048`（用户 turn 块，`agent_loop` 跑到无工具调用即停 + except 回滚）。
- 评估器范式模板：`main.py:_run_skill_reflection:2313`（消息副本 + 约束工具集 + 低 max_iterations + never-raise）。
- 结构化裁决范式：唯一工具强制调用，仿 `skill_create`（`src/core/handlers/skill.py`）。
- 纯模块边界范式：`src/core/retry.py`、`src/planning/skill_gen.py`。
- 配置范式：`[retry]`/`[skills]`/`[cache]` 的 dataclass + `_parse_*_config` 逐字段容错 + env 覆盖 + build。
- provider 工厂：`src/provider/factory.py`（按 model 构造便宜评估 provider）。
