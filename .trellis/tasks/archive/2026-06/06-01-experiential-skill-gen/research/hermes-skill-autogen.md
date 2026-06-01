# Hermes Agent 自动生成 skill 机制（调研结论，去营销化）

来源：Nous Research **Hermes Agent**（"The agent that grows with you"）官方文档 + GitHub。

## 核心三件套

去掉博客的"self-improving / 每 15 任务自评"等无据宣传后，真实机制只有三块：

1. **写能力工具 `skill_manage`** —— 让 agent 自主 create / update / delete skill，更新分 `patch`（定点小改，省 token，首选）和 `edit`（大改重写），另有 `write_file` / `remove_file` 管附属文件。**这是对外部框架唯一真正"新"的能力。**
2. **系统提示里的触发启发式**（非定时器）—— 满足任一即提示模型存 skill：
   - 完成复杂任务（**5+ 工具调用**）且成功后
   - 撞错/死胡同后找到可行路径
   - 用户纠正了它的做法
   - 发现非平凡工作流
3. **渐进式披露读取** —— `skills_list()` 开局注入（~3k token，仅名字+描述）→ `skill_view(name)` 按需加载全文 → `skill_view(name, file)` 按需加载附属文件。

## SKILL.md 格式与存储

- frontmatter：`name` / `description` / `version`（必填）+ 可选 `platforms` / `metadata.hermes.tags` / `category` / `config`。
- 正文：触发条件 + 步骤 + 踩坑 + 验证。
- 存 `~/.hermes/skills/`，按 category 分目录，是 single source of truth。

## 对 BareAgent 的映射 / 取舍

| Hermes 件 | BareAgent 现状 / 本任务取舍 |
|---|---|
| `skills_list()` 开局注入 | 已有 `SkillLoader.get_skill_list_prompt()` |
| `skill_view(name)` 按需 | 已有 `load_skill` 工具 |
| `skill_manage` **写** | **缺 → 本任务新增 create-only `skill_create`** |
| frontmatter markdown CRUD | 已在 `memory` 工具实证（范式可复用） |
| `~/.hermes/skills/` 用户级 | 本任务用 `~/.bareagent/projects/<slug>/skills/` 项目隔离 |
| 富 frontmatter（version/tags） | **不引入**，沿用 BareAgent 轻格式（目录名=skill 名，首行=描述） |
| 全自主静默写 | **改半自动**：自动起草草稿 → 用户 `/skill keep` 提升 |
| 自进化 update | **MVP 不做**，下次任务 |

## 关键风险（Hermes 被博客掩盖的部分）

- 自动写 = 攒垃圾 / 过拟合单次经历 / 写错 → 必须有质量闸（本任务用"草稿区 + 用户提升"）。
- 静默改自身未来行为 = 自主性跃升，需权限/可见性兜底。
- "省 40% 时间"等收益数字来自厂商/关联博客（TokenMix 等），对单人个人工具未独立验证。

## Sources

- https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/work-with-skills.md
- https://github.com/nousresearch/hermes-agent
