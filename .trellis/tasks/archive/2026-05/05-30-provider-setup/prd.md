# 交互式初始化配置（多 provider setup 向导）

## Goal

让用户**不再手动编辑配置文件**就能完成 provider/API 配置：通过交互式向导选择渠道（DeepSeek / ChatGPT(OpenAI) / Claude(Anthropic) / Qwen / GLM / 第三方 OpenAI 兼容云服务）、填入模型与 API key，并自动写入 `config.local.toml`。降低首次上手门槛。

## What I already know

* 入口 `bareagent = src.main:main`（pyproject `[project.scripts]`）。`main()` → `parse_args`（仅 `--provider/--model/--config` 三个 flag，无子命令）→ `load_config` → `create_provider` → `_run_stdio_session`。
* `src/provider/factory.py:create_provider` 硬编码只认三个 `name`：`anthropic`、`openai`、`deepseek`（deepseek 即 OpenAIProvider + 默认 `https://api.deepseek.com`）。未知 name 抛 `Unknown provider`。
* `openai` 路接受 `base_url` + `wire_api`，可指向任意 OpenAI 兼容端点——qwen / glm / 第三方都走这条路。
* key 处理（`factory.py:28`）：`api_key_env` 若以 `sk-` 开头 → 当明文 key 用；否则当 env 变量名 `os.getenv`。**坑**：qwen/glm 等非 `sk-` 前缀的 key 直接粘贴会被误判为变量名。
* 配置分层：`config.toml`（入库默认）→ `config.local.toml`（本地覆盖，已 git-ignore，`_deep_merge`）→ env → CLI（优先级递增）。写向导产物到 `config.local.toml` 最自然。
* `ProviderConfig` 字段：`name / model / api_key_env / base_url / wire_api`。

## Provider 预设端点（待实现时二次核对）

| 渠道 | provider 路由 | base_url | 默认 key env | 候选模型 |
|------|--------------|----------|-------------|---------|
| DeepSeek | openai 兼容 | https://api.deepseek.com | DEEPSEEK_API_KEY | deepseek-chat / deepseek-reasoner |
| ChatGPT (OpenAI) | openai 原生 | (默认) | OPENAI_API_KEY | gpt-4.1 / gpt-4o |
| Claude (Anthropic) | anthropic 原生 | (默认) | ANTHROPIC_API_KEY | claude-sonnet-4 / claude-opus-4 |
| Qwen (DashScope) | openai 兼容 | https://dashscope.aliyuncs.com/compatible-mode/v1 | DASHSCOPE_API_KEY | qwen-plus / qwen-max / qwen-turbo |
| GLM (Zhipu/BigModel) | openai 兼容 | https://open.bigmodel.cn/api/paas/v4 | ZHIPUAI_API_KEY | glm-4.6 / glm-4-plus |
| 第三方云服务 | openai 兼容 | 用户输入 | 用户输入 | 用户输入 |

## Assumptions (temporary)

* 交互向导用 prompt-toolkit（项目已依赖）实现，与现有 `src/ui/prompt.py` 输入层一致。
* 向导写入 `config.local.toml`，不动入库的 `config.toml`。
* MVP 只配「当前激活的单个 provider」，多 profile 切换是否纳入待与用户确认。

## Open Questions

* (none — 核心决策已全部 resolved)

## Resolved Decisions

* (Q1) ✅ 入口形态 = **独立 CLI 子命令 `bareagent init`** + **首次启动检测到无可用 key 时自动触发同一向导**。REPL 内运行时切换留作后续（Out of Scope）。需在 `parse_args` 引入子命令（subparser），`main()` 分派 `init` → 向导，无子命令 → 现有 REPL 流程。
* (Q2) ✅ 范围 = **单激活 provider**，向导覆盖式写 `config.local.toml` 的 `[provider]` 段，不改配置 schema、与现有 `load_config` 完全兼容。多 profile / 运行时切换 = Out of Scope。
* (Q3) ✅ key 落盘 = **默认明文写 git-ignored `config.local.toml`**，新增显式 `ProviderConfig.api_key` 字段（factory 优先 `api_key` → 回退 `api_key_env`），顺带修复非 `sk-` 前缀 key 被误判为 env 变量名的坑。向导提供"改用环境变量"的可选分支（写 `api_key_env`）。
* (Q4) ✅ live 验证 = **MVP 不做**，配完直接写盘，key 错误延迟到真正启动时报错。live 探活留作后续增强。
* (TOML writer) ✅ = **stdlib-only 外科式文本写入**（**零新依赖**，遵守 `quality-guidelines.md` "tomllib instead of tomli/tomlkit" + 依赖锁死 4 库的硬规范）。做法：读原文件文本 → `tomllib` 解析校验 → 仅把 `[provider]` 段（该行至下一个顶层 `[` 或 EOF）替换/插入为新文本块，其余 section 原样保留；写入走 `core/fileutil.py:atomic_write_text` 原子落盘。`[provider]` 全是简单字符串值无嵌套，文本块拼接风险可控。
  * **决策修正记录**：brainstorm 阶段曾选 tomlkit，实现前读 `quality-guidelines.md` 发现其明确点名禁止 tomlkit 且依赖需 PR 论证，遂改为 stdlib-only 文本写入（用户确认）。

## Requirements

* `bareagent init` 子命令进入交互式向导；首次启动检测到无可用 key 时自动触发同一向导。
* 向导菜单列出 6 类渠道：DeepSeek / ChatGPT(OpenAI) / Claude(Anthropic) / Qwen / GLM / 第三方 OpenAI 兼容（自定义 base_url）。
* 每个预设渠道带默认 base_url / 默认 key env / 候选模型，用户可回车采用默认或自定义。
* 向导收集 key（默认明文）后**仅改写** `config.local.toml` 的 `[provider]` 段，保留文件内其余 section（tomlkit）。
* 向导提供"改用环境变量"分支：写 `api_key_env` 而非明文 `api_key`。
* `ProviderConfig` 新增 `api_key` 字段；`factory.create_provider` 优先用 `api_key` → 回退 `api_key_env`，并修复非 `sk-` 前缀明文 key 的处理。
* 预设表（provider id → base_url / key env / 路由到 anthropic|openai）驱动 qwen / glm / 第三方走 OpenAIProvider + base_url。

## Acceptance Criteria

* [ ] `bareagent init` 跑完向导后，零手动编辑配置文件即可 `bareagent` 跑通选定渠道。
* [ ] 6 类渠道全部可配（含第三方自定义 base_url + 自定义 key）。
* [ ] 向导写 `[provider]` 时保留 `config.local.toml` 已有的其它 section（有测试覆盖）。
* [ ] 首次无 key 启动自动进入向导（有测试覆盖触发条件）。
* [ ] 单测：向导写出的 config.local.toml 能被 `load_config` 正确解析并构造出对应 provider。
* [ ] 单测：非 `sk-` 前缀的明文 key（qwen/glm 场景）经 `api_key` 字段被 factory 正确取用。

## Definition of Done (team quality bar)

* Tests added/updated（pytest，向导写出 → load_config 解析闭环）
* Lint / typecheck / CI green（ruff check + format）
* Docs 更新（docs/guide/ch03-configuration.md 增补向导用法）
* 不破坏现有手动改配置 / env / CLI 覆盖路径

## Technical Approach

* **入口分派**：`parse_args` 引入 subparser，新增 `init` 子命令；`main()` 中 `args.command == "init"` → 跑向导后 return；无子命令 → 现有 REPL 流程。保留 `--provider/--model/--config` 兼容。
* **首次自动触发**：`main()` 在 `create_provider` 前，检测当前 config 是否有可用 key（`api_key` 明文 或 `api_key_env` 对应 env 存在）；无则提示并进入向导，配完重新 `load_config`。
* **预设表**：新建模块（如 `src/provider/presets.py`）定义 `PROVIDER_PRESETS`：每项 = {id, 显示名, route(anthropic|openai), default_base_url, default_api_key_env, candidate_models}。向导与 factory 共用。
* **向导实现**：新建 `src/setup/wizard.py`（或 `src/cli/init.py`），用 prompt-toolkit（已依赖）做渠道选择 + 模型/base_url/key 录入 + 明文/env 分支。
* **写盘（stdlib-only）**：读 `config.local.toml` 文本（不存在则视作空）→ `tomllib` 解析校验 → 仅替换/插入 `[provider]` 段（name/model/base_url/api_key 或 api_key_env，全简单字符串值）→ `core/fileutil.py:atomic_write_text` 原子写回，保留其余 section 原文。**零新依赖**。
* **factory 改造**：`ProviderConfig` 加 `api_key: str | None`；`create_provider` 优先 `api_key`，否则走 `api_key_env`；qwen/glm/第三方经预设 route 到 OpenAIProvider + base_url。
* **依赖**：无新增（遵守 quality-guidelines "stdlib-first / 禁 tomlkit / 依赖锁 4 库"）。

## Decision (ADR-lite)

**Context**: 项目原本只能手改 `config.toml`/`config.local.toml` 配 provider，factory 仅硬编码 3 个 name，新用户上手门槛高、qwen/glm 无法直配且有 key 处理坑。
**Decision**: 加 `bareagent init` 向导（+首次无 key 自动触发），预设表驱动 6 类渠道，key 默认明文写 git-ignored config.local.toml（新增 `api_key` 字段），tomlkit 保段写回。单激活 provider、不做 live 验证、不做运行时切换。
**Consequences**: 上手门槛大幅降低；新增 tomlkit 依赖；key 明文落本地（git-ignore 兜底，提供 env 分支）；多 profile 切换 / live 验证留作后续，schema 不返工。

## Out of Scope (explicit)

* 多 provider profile 运行时热切换 / REPL `/provider` 切换命令（后续任务）。
* 保存前 live 验证 key 有效性（后续增强）。
* OS keyring / 加密存储 key。
* `.env` 文件加载机制。

## Technical Notes

* 关键文件：`src/main.py`（parse_args / main / load_config / ProviderConfig）、`src/provider/factory.py`、`src/provider/presets.py`(新)、`src/setup/wizard.py`(新)、`config.toml`、`docs/guide/ch03-configuration.md`、`pyproject.toml`。
* prompt-toolkit 已在 deps，可复用其 prompt / radiolist；注意 Windows 下交互输入与 `src/ui/prompt.py` 的兼容。
* 预设端点/模型名见上「Provider 预设端点」表，实现时二次核对 qwen(DashScope)、glm(BigModel) 的 base_url 与 key env。
* 测试注意：交互向导需把输入层抽象成可注入（避免测试时真起 prompt-toolkit），写盘/解析闭环用 tmp_path。
