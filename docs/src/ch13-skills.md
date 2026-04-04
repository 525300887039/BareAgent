# 技能系统

BareAgent 的技能系统不是“安装后自动执行一段代码”的插件框架，而是一套非常轻量的文本能力加载机制：

- 先扫描 `skills/*/SKILL.md`
- 把技能清单摘要放进系统提示
- 需要细节时，再通过 `load_skill` 把某个 `SKILL.md` 的全文读进上下文

实现位于 `src/planning/skills.py`。

## 13.1 技能发现

技能发现由两层组成：

- `resolve_skills_dir()`：先决定技能目录在哪
- `SkillLoader.scan()`：再从这个目录里收集技能元数据

### 技能目录解析

`resolve_skills_dir()` 的查找顺序是：

1. 如果设置了 `BAREAGENT_SKILLS_DIR`，优先用它
2. 否则尝试项目内的候选目录
3. 返回第一个存在的目录

当前内置候选是相对于 `src/planning/skills.py` 的两个位置：

- `.../skills`
- `.../src/skills`

在当前仓库布局下，最终命中的通常是项目根目录下的：

```text
skills/
```

如果这些候选都不存在，`resolve_skills_dir()` 仍会返回第一个候选路径作为默认位置，只是该目录此时可能还没有内容。

### 扫描规则

`SkillLoader.scan()` 只扫描一层目录下的：

```text
*/SKILL.md
```

也就是说，一个技能最基本的约定是：

```text
skills/<skill-name>/SKILL.md
```

当前实现不会：

- 递归扫描更深层子目录
- 识别别的文件名
- 自动解析 JSON/YAML 元数据

### `SkillMeta`

扫描结果会被整理成 `SkillMeta` 数据类：

| 字段 | 含义 |
|------|------|
| `skill_name` | 技能目录名 |
| `description` | 从 `SKILL.md` 中提取的简短描述 |
| `path` | `SKILL.md` 的绝对路径 |

### 描述提取规则

`SkillLoader` 提取描述时不会读懂复杂 front matter。当前规则非常简单：

- 跳过空行
- 跳过 Markdown 标题行（以 `#` 开头）
- 取第一条剩下的非空行作为描述

例如下面这个文件：

```md
# Git Workflow

Use this skill when you need to prepare commits...
```

提取出来的描述就是：

```text
Use this skill when you need to prepare commits...
```

## 13.2 技能加载

技能系统真正暴露给模型的入口是 `load_skill` 工具。

### `load_skill`

它的 schema 很小，只接受一个参数：

| 参数 | 说明 |
|------|------|
| `skill_name` | 技能目录名 |

调用成功后，返回值不是解析后的结构体，而是该技能 `SKILL.md` 的完整文本内容。

也就是说，BareAgent 当前的“加载技能”本质上就是：

- 先定位到对应目录
- 再把 Markdown 文本原样读出来

### 未知技能

如果技能名不存在，`load()` 会抛出：

```text
ValueError("Unknown skill: ...")
```

这让模型必须先根据技能列表做出一个有效选择，而不是随便猜目录名。

### 为什么要分“清单摘要”和“全文加载”

主 REPL 初始化系统提示时，不会把每个技能全文直接塞进上下文。它只会调用：

```python
skill_loader.get_skill_list_prompt()
```

生成一段技能清单摘要，例如：

```text
Available skills (load the full SKILL.md only when you need the details):
- git: Use this skill when you need to prepare commits...
- test: Use this skill when writing or reviewing tests...
```

然后把这段摘要拼进系统提示。

这样做的目的很明确：

- 模型一开始知道“有哪些技能”
- 但不会被所有技能全文占满上下文
- 真正需要时再用 `load_skill` 拉取详情

所以技能系统的重点不是“让所有知识常驻”，而是“按需读取”。

## 13.3 内置技能

当前仓库自带 3 个技能目录：

| 技能 | 目录 | 描述摘要 |
|------|------|------|
| `code-review` | `skills/code-review/` | Use this skill when you need a practical review checklist for correctness, safety, performance, and maintainability. |
| `git` | `skills/git/` | Use this skill when you need to prepare commits, choose a branch name, or check whether a change is ready to land. |
| `test` | `skills/test/` | Use this skill when writing or reviewing tests for agent loops, tool handlers, prompt assembly, or filesystem behavior. |

它们都只是 Markdown 指南，不包含可执行代码。

### 这三个技能的定位

从内容上看，当前内置技能分别偏向：

- `code-review`：审查顺序、风险检查项、输出格式
- `git`：分支命名、Conventional Commits、提交前检查
- `test`：测试设计、边界条件、mock 策略、断言方式

这些内容本身并不神秘，关键在于：

- 它们被扫描进技能清单
- 可以由模型按需 `load_skill`
- 还会随打包流程一起进入发布产物

### 发布时包含技能目录

`pyproject.toml` 已显式把 `skills/` 加入：

- wheel 的 `force-include`
- sdist 的 `include`

因此技能目录不只是本地开发资源，也是分发包的一部分。

## 13.4 自定义技能编写指南

BareAgent 当前对自定义技能的要求非常少，这也是它容易扩展的原因。

### 最小结构

新增一个技能至少需要：

```text
skills/
  my-skill/
    SKILL.md
```

其中目录名就是将来传给 `load_skill(skill_name=...)` 的名字。

### 推荐写法

为了让 `SkillLoader` 和模型都更容易使用，自定义 `SKILL.md` 最好具备：

1. 一个清晰标题
2. 一条放在标题后的简短描述
3. 若干具体、可执行的规则或流程

例如：

```md
# API Review

Use this skill when evaluating public API stability and migration risk.

- Check backward compatibility first.
- Prefer additive changes.
- ...
```

这样扫描后：

- 标题用于人类阅读
- 第二行描述会进入技能清单摘要
- 后续正文在 `load_skill` 时按需注入

### 当前能力边界

BareAgent 现在不会自动做这些事：

- 不会执行技能目录里的脚本
- 不会解析相对资源依赖
- 不会对技能名做别名映射
- 不会热更新已经注入进当前系统提示的技能清单

所以它更像“按需加载的知识卡片系统”，而不是“插件运行时”。

## 小结

BareAgent 的技能系统可以概括为：

1. 用 `scan()` 自动发现 `skills/*/SKILL.md`
2. 用 `get_skill_list_prompt()` 把技能摘要注入系统提示
3. 用 `load_skill` 在需要时读取某个技能全文
4. 用最少约定支持自定义扩展

下一章会继续介绍另一项“延迟完成”的能力：当某个 shell 命令或子任务不适合同步阻塞当前回合时，BareAgent 如何把它放进后台线程执行，并在后续回合把结果重新注入上下文。
