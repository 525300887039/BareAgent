# 经验式技能生成：agent 从经验自动长 skill

## Goal

让 BareAgent 的 agent 在完成复杂任务后，能把"这次怎么做成的"（步骤 + 踩坑 + 验证方式）沉淀成一个可复用的 SKILL.md，下次遇到同类任务自动获得这份经验。对标 Nous Research **Hermes Agent** 的 `skill_manage` 自动生成 skill 能力，但裁剪掉其全自主、自进化的高风险部分，落到适合单人个人工具的最小可用档。

## What I already know

**Hermes 的实现（调研结论，去营销化）**：
- 核心 = 一个写能力工具 `skill_manage`（create/update[patch|edit]/delete + write_file/remove_file）+ 一段"复杂任务后存 skill"的系统提示触发启发式 + 渐进式披露读取。
- 触发启发式（满足任一）：5+ 工具调用且成功 / 撞错后找到可行路径 / 用户纠正了做法 / 发现非平凡工作流。
- SKILL.md：frontmatter `name`/`description`/`version`（+可选 platforms/tags/category/config），正文 = 触发条件 + 步骤 + 踩坑 + 验证。
- 存 `~/.hermes/skills/`，按 category 分目录。读取三层：`skills_list()` 开局注入(~3k token) → `skill_view(name)` 按需 → `skill_view(name, file)` 附属文件。
- 第三方博客吹的"每 15 任务自评""self-improving loop"**官方文档无据**，实际更新是机会主义 patch，不是定时调度。

**BareAgent 现状（已有一大半基建）**：
- 读取侧已完备：`SkillLoader.scan()` 扫 `skills/*/SKILL.md`、`get_skill_list_prompt()` 开局注入名字+描述、`load_skill` 工具按需加载全文（`src/planning/skills.py` + `make_skill_handlers`）。等价于 Hermes 的 skills_list + skill_view 两层渐进披露。
- 写 markdown 文件的 CRUD 范式已实证：`memory` 工具（单工具多子命令 view/create/str_replace/insert/delete/rename，handler `core/handlers/memory.py:run_memory` 委派 `MemoryManager`，路径经 `core/sandbox.py:safe_path`，原子写 `atomic_write_text`，入 `PermissionGuard.SAFE_TOOLS`）。
- 故对本仓库唯一真正新增的能力 = **"让 LLM 能写 skill"**（现有 `load_skill` 只读）。

**关键差异 / 风险点**：
- BareAgent 的 `skills/` 是**仓库级、已 checked-in**（code-review/git/test 三个手写 skill），而 Hermes 是用户级 `~/.hermes/skills/`。自动生成的 skill 若写进仓库 skills/ 会污染版本控制 → 大概率要写到独立的用户级/ gitignore 目录。
- 自动写 = 会攒垃圾/过拟合/写错，需剪枝或人工确认兜底，否则污染开局列表（吃 token）+ 误导后续。
- 静默自主写文件改变自身未来行为，是自主性跃升，需过 PermissionGuard + 用户可见。

## Assumptions (temporary)

- 复用现有 `MemoryManager` 的文件 CRUD 范式，新增一个指向 skills 目录的写工具，而非从零造。
- 读取侧零改动，直接吃现成的 `load_skill` + 开局列表。
- 自动生成的 skill 存到独立目录（用户级或 gitignored），与仓库手写 skill 分离。

## Open Questions

- ~~[决策1] 自主性档位~~ → **已定：B 自动起草 + 草稿区 + 用户提升**。模型在复杂任务收尾自动生成 skill 草稿落 pending 区（不进 live 路径、零权限摩擦），turn 末尾一行提示 `/skill keep`，不提升则过期。挡住过拟合/写错污染正式 skill 集；保留升级到全自动的平滑路径。
- ~~[决策2] 存储位置~~ → **已定：(a) 用户级 + 项目隔离**。live 生成 skill 放 `~/.bareagent/projects/<workspace-slug>/skills/`（复刻 `derive_memory_slug` 目录约定，不进版本控制、项目隔离）；pending 草稿放同目录下 `.pending/`，提升即挪到正式区。读取侧 `SkillLoader` 加**第二个扫描根**（生成目录）：仓库 skills/=正典层、生成 skill=习得层，两层都列出都可 `load_skill`。纠正"读取侧零改动"为"小改 multi-root"。
- ~~[决策3] 触发机制~~ → **已定：B 循环驱动硬信号 + 引导注入**。`agent_loop` turn 自然收尾时跑**纯逻辑启发式**判断；命中则注入"把刚才工作流起草成 skill"引导，模型调写工具落 `.pending/`。**复杂度信号 = 双条件 AND**：`累计工具调用 ≥ 5` 且 `累计用户回复 ≥ 3`（均可配，默认 5 / 3；按 ≥ 实现）；计数从会话开始/上次起草后累加，**触发即重置**（一段多轮工作流打包成一个 skill）。turn 失败/中断不触发。额外反思 LLM 调用用户接受，严格 gate + 配置可关 `[skills] auto_generate`（默认开）。"撞错恢复""用户纠正"等糊信号列入 Out of Scope。
- ~~[决策4] 写工具形态~~ → **已定：A create-only**。模型端只暴露"把一份 SKILL.md（name/description/正文）写进 `.pending/`"单一动作；读已有 skill 走现成 `load_skill`，不让写工具兼读。草稿写 `.pending/`（非 live 路径）→ 进 `PermissionGuard.SAFE_TOOLS` 不弹确认；提升到 live 由用户手动命令把关。delete/rename 不给模型自主用。
- ~~[决策5] 自进化~~ → **已定：MVP 不做，下次任务再做**。MVP 闭环 = create 草稿 → 用户提升 → 读取生效。自进化（相似度匹配 + update 已有 skill）连同其"改坏已有 skill"风险一并推迟。

## Requirements

**触发与起草（决策 1 + 3）**
- `agent_loop` 主循环维护累计计数器（`tool_calls`、`user_replies`）；turn 自然收尾（stop、无更多工具调用、未失败/中断）时跑纯逻辑启发式 `should_draft_skill(tool_calls, user_replies, cfg)`。
- 双条件 AND 命中（默认 `tool_calls ≥ 5` 且 `user_replies ≥ 3`，均可配）→ 注入一句"把刚才工作流起草成 skill"引导，触发一次**额外反思 LLM 调用**，模型调 create-only 写工具产出草稿；随后**重置计数器**。
- 自动生成总开关 `[skills] auto_generate`（默认 true），关闭时计数器/启发式/反思调用全部短路（行为与未接此特性前一致）。

**写工具（决策 4）**
- 新增单一 create-only 写工具（暂名 `skill_create`）：入参 `name` / `description` / `body`，把一份符合 `SkillLoader` 格式的 SKILL.md 写到 `<生成根>/.pending/<name>/SKILL.md`（原子写、`safe_path` 沙箱）。
- 入 `PermissionGuard.SAFE_TOOLS`（草稿区非 live 路径，不弹确认）；不暴露 delete/rename/update。
- 子代理隔离：写工具不给只读子代理类型（explore/plan/code-review），自动生成触发**仅主循环**（子代理不挂，比照 hooks `hook_engine` 不传子代理）。

**存储与读取（决策 2）**
- 生成 skill 根 = `~/.bareagent/projects/<workspace-slug>/skills/`（复用 `derive_memory_slug` 派生 slug，可配 `[skills] dir`）；pending 草稿在其下 `.pending/`。
- `SkillLoader` 加**第二个扫描根**（生成根的 live 区，非 `.pending/`）：仓库 `skills/` = 正典层、生成 skill = 习得层，两层都进开局列表、都可 `load_skill`；同名冲突仓库正典优先。
- 生成 SKILL.md 必须匹配现有格式：`<skill-name>/SKILL.md`，描述 = 首个非空非 `#` 行（`SkillLoader._extract_description` 契约）。

**用户侧命令（决策 1 提升机制）**
- `/skill`（无参）= 列出 live + pending + 用法；`/skill list` 同。
- `/skill keep <name>` = 把 `.pending/<name>/` 提升（挪）到 live 生成根。
- `/skill discard <name>` = 删除一个 pending 草稿。
- pending 数量软上限（默认 10）：超出按最旧裁剪，避免无限堆积（"不提升即过期"的最小实现，计数裁剪而非时间 TTL）。

## Acceptance Criteria

- [x] `should_draft_skill` 纯函数单测：单条件不触发、双条件 AND 命中、阈值可配、失败 turn 不触发。
- [x] 计数器累加 + 触发即重置 行为单测。
- [x] `skill_create` 把草稿写到 `.pending/<name>/SKILL.md`，格式可被 `SkillLoader` 解析（描述抽取正确）。
- [x] `SkillLoader` 多根扫描：仓库 skill 与生成 live skill 同时出现在列表、均可 `load_skill`；同名仓库优先。
- [x] `.pending/` 草稿**不**进开局列表、**不**可被 `load_skill` 加载（未提升前不生效）。
- [x] `/skill keep` 把草稿挪到 live 后即可被列出+加载；`/skill discard` 删除草稿。
- [x] pending 软上限裁剪生效。
- [x] `[skills] auto_generate = false` 时全链路短路，无额外 LLM 调用、无计数、行为回退。
- [x] 子代理不触发自动生成、拿不到 `skill_create`。
- [x] `PermissionGuard`：`skill_create` 在 SAFE_TOOLS（不弹确认）；`/skill keep` 是用户命令（人把关）。

## Implementation Note (优于 PRD 原草案的一处精化)

`skill_create` **最终不进全局 `TOOL_SCHEMAS`/`DEFERRED_TOOLS`**，只在 `main.py:_run_skill_reflection` 的隔离反思 `agent_loop` 调用里以 `tools=[SKILL_CREATE_TOOL_SCHEMA]` 暴露。好处：一处实现就同时拿到"主循环触发、子代理拿不到、`auto_generate=false` 全链路短路"——因为工具在其它语境下**根本不存在**，故**无需**改 `agent_types._READ_ONLY_DEFAULTS` 黑名单。反思跑在**消息副本**上（`skill_gen=None`、低 `max_iterations`），真实历史/turn 返回值零污染；模型可回 "no skill" 拒绝（第二道质量闸）。

实测：新增 `tests/test_experiential_skill_gen.py` 29 测试全过；全量 894 passed / 3 skipped（本机 localhost flaky）/ 0 回归；ruff + pyright clean；无新增依赖。

## Definition of Done (team quality bar)

- Tests added/updated（纯逻辑可单测：触发判定、frontmatter 生成、存储路由）
- ruff / pyright green
- CLAUDE.md 架构段更新（若引入新模块/工具）
- 无新增重依赖；自动写有权限/可见性兜底

## Out of Scope (explicit)

- **自进化 / update 已有 skill**（决策 5，下次任务）：相似度匹配、`patch`/`edit` 更新、改写 live skill。
- 定时自评循环（Hermes 博客的"每 15 任务"，官方无据）。
- "撞错恢复""用户纠正了做法"等糊复杂度信号（误判率高，MVP 只用工具调用数 + 用户回复数）。
- 自动提升草稿到 live（保留用户 `/skill keep` 这道闸；将来可加 config 升到全自动）。
- 全局跨项目 skill 层、跨设备同步、benchmark 化收益度量、frontmatter 富 schema（version/tags/category/platforms，沿用 BareAgent 现有"目录名 + 首行描述"轻格式，不引入 Hermes frontmatter）。
- 时间型 TTL 过期（用计数软上限替代）。

## Technical Approach

**核心新增（拟）**
- `src/planning/skill_gen.py`（纯逻辑模块，无 LLM/loop 依赖、可单测，比照 `retry.py` 范式）：`SkillGenConfig`（阈值/开关）+ `should_draft_skill(...)` + 计数器/重置逻辑 + draft 路径路由。
- `skill_create` 工具 schema（`core/schema.py`）+ handler（`core/handlers/` 薄封装委派写盘，比照 `memory.py:run_memory`）。
- `SkillLoader`（`src/planning/skills.py`）扩展为多扫描根（仓库根 + 生成 live 根），同名仓库优先；新增 pending 提升/裁剪/列举辅助（或拆 `SkillStore`）。
- `agent_loop`（`src/core/loop.py`）：turn 收尾处接入计数 + 启发式 + 反思引导注入（仅主循环，子代理不挂；可选 `skill_gen` 参数，默认 None = 旧行为，向后兼容）。
- `main.py`：`[skills]` 配置解析（`auto_generate`/阈值/`dir`，逐字段容错 + env 覆盖 `BAREAGENT_SKILLS_AUTO_GENERATE`）、`/skill` 命令派发（list/keep/discard，比照 `_dispatch_*` never-raise 范式）、生成根 slug 派生复用 `derive_memory_slug`。
- `PermissionGuard.SAFE_TOOLS` 加 `skill_create`；`AgentType` 黑/白名单排除子代理写工具（比照 `memory_writable`）。
- `config.toml [skills]` + CLAUDE.md 架构段。

**向后兼容**：`auto_generate=false` 或未注入 `skill_gen` ⇒ 全链路短路，无额外调用、无新文件副作用、行为与未接特性前一致。

## Decision (ADR-lite)

**Context**：对标 Hermes Agent 的自动生成 skill（"agent 从经验长技能"），但其全自主静默写 + 自进化对单人个人工具风险过高，且 BareAgent 已具备 skill 读取层（渐进披露）+ memory 的 markdown CRUD 范式，真正缺的只有"让 LLM 写 skill"。

**Decision**：做"自动起草 + 草稿区 + 用户提升"的半自动闭环（决策 1=B）。生成 skill 用户级项目隔离存储（决策 2=a）；循环驱动双条件 AND 启发式触发（决策 3，tool_calls≥5 且 user_replies≥3，可配可关，接受额外反思 LLM 调用）；create-only 写工具（决策 4=A）；MVP 不做自进化（决策 5，下次任务）。

**Consequences**：以"用户提升"一道闸换取质量可控、规避自动写攒垃圾/信任风险；额外反思调用带来 token 成本（严格 gate + 可关）；读取侧从单根扩为多根（小改）；保留升级到全自动 + 自进化的平滑路径，无返工。

## Technical Notes

- 关键现有文件：`src/planning/skills.py`（SkillLoader/load_skill）、`src/memory/persistent.py`（MemoryManager CRUD 范式）、`src/core/handlers/memory.py`（薄 handler 委派）、`src/core/tools.py`（工具注册 + DEFERRED_TOOLS）、`src/core/sandbox.py:safe_path`、`src/permission/guard.py`（SAFE_TOOLS / 写工具档位）。
- 子代理隔离参照：`memory_writable` / `AgentType` 现有范式。

## Research References

- [`research/hermes-skill-autogen.md`](research/hermes-skill-autogen.md) — Hermes skill_manage 机制（待落盘，可选）

## Decision (ADR-lite)

- 待 brainstorm 收敛后记录。
