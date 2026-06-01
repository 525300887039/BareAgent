# skill 自进化：agent 更新已有 skill

## Goal

让 agent 在反思起草 skill 时，识别"这次经验是已有某个生成 skill 的改进版"，从而**更新/取代那个 skill**（补充新边界、改进步骤、更好的验证），而不是堆一个近似重复的新 skill。对标 Hermes Agent 的 `skill_manage` update（patch/edit）+ "self-improving loop"。承接上一任务 `06-01-experiential-skill-gen` 的 decision 5（当时明确推迟到本任务）。

## What I already know（承接上个任务的既有机制）

- **触发 + 反思已就绪**：复杂多轮任务（工具≥5 且 回复≥3）收尾 → `main.py:_run_skill_reflection` 在消息副本上跑隔离 `agent_loop`，当前只暴露 `skill_create`（写 `.pending/<slug>/SKILL.md`）。
- **同名替换已半支持**：`SkillStore.promote(name)` 已经 `if dest.exists(): rmtree(dest)` 再 move —— 即**用同名草稿提升即可取代 live skill**。`create_draft(name)` 同名会覆盖 pending。
- **多根读取**：`SkillLoader` 扫 仓库 `skills/`（正典层）+ 生成 live 根（习得层），**正典优先**（同名 generated 被 shadow）。
- **读工具现成**：`make_skill_handlers(loader)` = `{"load_skill": loader.load}` + `LOAD_SKILL_TOOL_SCHEMAS`，可注入反思调用让模型读已有 skill 全文。

**真正缺的（本任务核心）**：反思模型现在对已有 skill **内容是盲的**（只暴露 skill_create，看不到已有 body），无法做"基于现有内容改进"。补上"读 + 匹配 + 同名取代"即闭环。

## Assumptions (temporary)

- 复用现有 create→promote 机制：进化产物 = 同名修订草稿落 `.pending/`，用户 `/skill keep` 提升替换 live（与 create 流程一致、安全）。
- 自进化范围 = 仅生成习得层；仓库正典只读不动（generated 同名会被 shadow，无意义）。
- MVP 不做向量相似度匹配，靠 LLM 判断（注入已有生成 skill 列表/内容到反思上下文）。

## Open Questions

- ~~[决策1] 进化产物去向 / 安全闸~~ → **已定：A 修订走 pending 闸**。模型把改进版作为**同名修订草稿**落 `.pending/`，`/skill keep <name>` 提升时 `promote` 同名 `rmtree`+move 取代 live。零新机制（复用上个任务的闸），进化比新建更危险（动的是已生效的 skill）故更该有闸；保留后续给特定场景开自动提升的平滑路径。
- ~~[决策2] 匹配机制~~ → **已定：A LLM 判断**。反思上下文注入**已有生成 skill 名+描述清单**（开局列表那套，廉价有界，只列习得层=可进化候选），反思调用加只读 `load_skill`（现成 `make_skill_handlers`/`LOAD_SKILL_TOOL_SCHEMAS`）。模型：看候选 → load_skill 读全文 → 同名 skill_create 写改进版（进化）/ 新名（新建）。无词法/向量预筛（读写边界仍干净：load_skill 读、skill_create 写）。
- ~~[决策3] 更新工具形态~~ → **已定：A 复用 `skill_create` 同名全量重写**。模型 load_skill 读旧全文 → 同名 skill_create 写改进 body → pending → keep 取代。零新工具/handler/schema。配套 `/skill list` 给 pending 中与 live 同名的草稿标注 `(修订 live 'foo')`。patch/skill_update 留后续（skill 小，全量重写 token 浪费可忽略，避免 patch mismatch 风险）。
- ~~[决策4] 范围 / 正典保护~~ → **已定：A 拦正典同名**。`skill_create` 写盘前检查 slug 撞仓库正典名即返回 Error；反思把正典名集合（`SkillLoader` 只扫 `skills_dir`）作 `reserved_names` 传 handler。对新建+进化都生效（生成层永不与正典撞名的好卫生）。范围 = 只进化生成习得层，正典只读不动（可读作参考，不可覆盖/同名）。
- ~~[决策5] 历史 / 回滚~~ → **已定：A 不留备份**。`promote` 照旧 rmtree+move 直接替换。安全闸 = 用户提升那一步（`/skill list` 标注修订，知情覆盖）+ skill 廉价可再生。"版本历史/回滚/`/skill restore`"整体列入 Out of Scope 作连贯后续单元（不塞半套）。

## Requirements

**反思调用增强（决策 1/2/3）**
- `_run_skill_reflection` 的反思 `agent_loop` 工具集从 `[skill_create]` 扩为 `[skill_create, load_skill]`（`load_skill` 只读，已在 SAFE_TOOLS）；handler 加 `load_skill`（复用会话 `skill_loader.load`，可读正典+生成两层全文）。
- 反思 user 消息注入**已有生成 skill 候选清单**（名+描述，只列习得层）。candidates 由 `SkillLoader(skill_store.root).scan()` 得（生成 live，pending 天然不入）；空则退化为纯 create（向后兼容）。
- 更新指令（`skill_gen.render_reflection_prompt(candidates)`）：若这次工作流是清单中某 skill 的改进 → `load_skill` 读其全文 → 用**该 skill 的精确名** `skill_create` 写改进版（= 取代）；否则用新名（= 新建）；**绝不复用仓库正典名**。

**正典保护（决策 4）**
- `run_skill_create` 加 `reserved_names: set[str] | None`；slug 命中即返回 `Error:`（不写盘）。
- 反思把仓库正典名集合传入：`SkillLoader.canon_skill_names()`（只扫 `skills_dir` 的 `*/SKILL.md`）。守卫对新建+进化都生效。

**提升 / UX（决策 1/3）**
- 进化 = 同名修订草稿落 `.pending/`；`/skill keep <name>` 经现有 `promote`（同名 rmtree+move）取代 live。零 store 改动。
- `/skill list` 给 pending 中**与某个 live 同名**的草稿标注 `(revision of live '<name>')`，提升前可见是覆盖。

**范围 / 安全（决策 4/5）**
- 仅进化生成习得层；仓库正典只读不动。直接替换不留备份（回滚后续）。
- 触发/计数/安全闸/子代理隔离全部沿用上个任务，无新增（反思仍主循环 only、消息副本、`auto_generate=false` 全短路）。

## Acceptance Criteria

- [x] `render_reflection_prompt`：有候选时注入清单 + 进化指令；空候选退化为纯 create 指令（与上个任务一致）。
- [x] `run_skill_create` `reserved_names` 命中正典名 → 返回 Error、不写盘；未命中正常写；`reserved_names=None` 不拦。
- [x] `SkillLoader.canon_skill_names()` 只返回仓库 `skills_dir` 的 skill 名（不含生成层）；缺目录返回空集。
- [x] 同名修订草稿 → `promote` 取代 live：旧 live 被替换、新内容可 `load_skill` 加载。
- [x] `/skill list`：pending 中与 live 同名者标注 `(revision of live ...)`；非同名不标注。
- [x] 候选清单只含生成 live、不含 pending、不含正典（`SkillLoader(store.root).scan()`）。
- [x] 向后兼容：无生成 skill 时反思**工具集 + user 消息字节级一致**（纯 create——`load_skill` 仅在有候选时才挂）。

## Implementation Note

- **优于原草案的一处**：`load_skill` 只在 `candidates` 非空时才加入反思工具集（不是无条件加），使"无生成 skill"场景的反思请求与上个任务**字节级一致**（工具集 + 消息都不变），向后兼容更干净。
- 守卫用 `derive_skill_slug` 归一后再比 `reserved_names`（`name="Git"` → `git` 命中正典），避免大小写/空格绕过。
- 实测：新增 `tests/test_skill_self_evolution.py` 10 测试全过；全量 904 passed / 3 skipped（本机 localhost flaky）/ 0 回归；ruff + pyright clean；无新增依赖。

## Definition of Done (team quality bar)

- Tests added/updated（纯逻辑/store 可单测：同名取代、范围拒绝、匹配上下文构造）
- ruff / pyright green
- CLAUDE.md 架构段更新（自进化）
- 无新增重依赖；进化沿用既有安全闸（不静默改 live）

## Out of Scope (explicit)

- **版本历史 / 回滚 / `/skill restore` / `.bak`**（决策 5，连贯后续单元，不塞半套）。
- `skill_update` patch/edit 工具（决策 3，复用 skill_create 同名全量重写；skill 小，token 浪费可忽略）。
- 向量/语义相似度匹配、词法预筛（决策 2，LLM 判断足够）。
- 自动提升进化版（保留用户 `/skill keep` 闸；决策 1）。
- 直接改写 live（决策 1）、改写仓库正典（决策 4）。
- 富 frontmatter version 字段化、定时自评、跨项目共享（沿用上个任务 Out of Scope）。

## Technical Approach

**改动（全部围绕反思调用 + handler 守卫，store/loop/触发零改或极小改）**
- `src/planning/skill_gen.py`：新增 `render_reflection_prompt(candidates: list[tuple[str,str]]) -> str`（有候选注入清单 + 进化指令，空则返回纯 `DRAFT_INSTRUCTION`）；`DRAFT_INSTRUCTION` 文案补"匹配则复用精确名取代/否则新名/不复用正典名"。
- `src/planning/skills.py`：`SkillLoader` 加 `canon_skill_names() -> set[str]`（只扫 `self.skills_dir`）。
- `src/core/handlers/skill.py`：`run_skill_create` 加 `reserved_names: set[str] | None`，slug 命中返回 Error。
- `src/main.py:_run_skill_reflection`：candidates = `SkillLoader(skill_store.root).scan()` 的 (name, description)；reserved = `skill_loader.canon_skill_names()`；反思 tools = `[SKILL_CREATE_TOOL_SCHEMA, *LOAD_SKILL_TOOL_SCHEMAS]`，handlers 加 `load_skill=skill_loader.load` 且 `skill_create=partial(run_skill_create, store=, reserved_names=)`；user 消息用 `render_reflection_prompt(candidates)`；`max_iterations` 略升（load_skill+skill_create+收尾，设 6）。
- `src/main.py:_print_skill_list`：pending 与 live 同名者标注 `(revision of live '<name>')`。
- `CLAUDE.md`：经验式技能生成段补"自进化"。

**向后兼容**：无生成 skill（candidates 空）⇒ `render_reflection_prompt` 退回纯 `DRAFT_INSTRUCTION`、reserved 仅挡正典名（既有行为不变）；`reserved_names=None` ⇒ handler 不拦。

## Decision (ADR-lite)

**Context**：承接 `experiential-skill-gen` decision 5。已有 create→promote 同名替换机制其实半支持进化，真正缺的是"反思模型对已有 skill 内容是盲的"。

**Decision**：补"读 + LLM 匹配 + 同名取代"闭环：反思加只读 `load_skill` + 注入生成 skill 候选清单（决策2），复用 `skill_create` 同名全量重写（决策3），修订仍走 pending→用户 keep 取代（决策1），`reserved_names` 挡正典同名死 skill（决策4），不留备份（决策5）。

**Consequences**：几乎零新机制（复用 promote/SkillLoader/load_skill），新增面只有"反思多挂只读工具 + 注入清单 + handler 一道守卫 + list 标注"；进化的覆盖不可逆由用户提升闸 + 修订标注兜底；patch/回滚/相似度均留为自包含后续单元。

## Technical Notes

- 关键现有文件：`src/planning/skill_store.py`（create/promote 同名替换）、`src/planning/skills.py`（多根 + load）、`src/core/handlers/skill.py`（skill_create 工具/schema）、`src/planning/skill_gen.py`（DRAFT_INSTRUCTION）、`src/main.py:_run_skill_reflection`（反思调用，注入 tools/handlers 处）。
- 复用点：`make_skill_handlers` / `LOAD_SKILL_TOOL_SCHEMAS`（给反思加只读）、`SkillStore.promote` 同名替换语义、`SkillLoader` 正典优先。

## Research References

- [`../06-01-experiential-skill-gen` (已归档)] — 上个任务 PRD/research（Hermes skill_manage update patch/edit、self-improving loop 调研）。

## Decision (ADR-lite)

- 待 brainstorm 收敛后记录。
