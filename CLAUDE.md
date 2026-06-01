# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

BareAgent 是一个纯 Python 终端代码智能体，支持可插拔 LLM 提供商、细粒度权限控制、多智能体协调和可扩展技能系统。基于 Python 3.12+，使用 Hatchling 构建。

## 常用命令

```bash
# 安装（可编辑模式）
uv pip install -e ".[dev]"
# 可选追踪后端
uv pip install -e ".[langfuse]"        # Langfuse
uv pip install -e ".[otel]"            # OpenTelemetry
uv pip install -e ".[all-tracing]"     # 全部

# 运行
bareagent                          # 或: python -m src.main
bareagent --provider anthropic --model claude-sonnet-4-20250514
bareagent --config ~/my_config.toml

# 测试
pytest                             # 全部测试
pytest tests/test_loop.py          # 单个文件
pytest tests/test_loop.py -k "test_name"  # 单个测试

# 代码检查与格式化
ruff check src tests               # 检查
ruff check --fix src tests          # 自动修复
ruff format src tests               # 格式化
```

## 架构

### 核心智能体循环 (`src/core/loop.py`)
`agent_loop()` 是中央调度器：调用 LLM → 解析工具调用 → 权限检查 → 执行处理器 → 收集结果。最多迭代 `max_iterations`（200）次。支持流式输出和长对话消息压缩。

### LLM 重试策略 (`src/core/retry.py`，ROADMAP 4.2)
瞬时性 LLM 调用失败（rate limit / 网络超时 / 5xx / overloaded）自动指数退避重试，不可重试错误（认证 / 400 bad request / 模型不存在 / 未知异常）立即上抛——不掩盖真正的配置错误。`retry.py` 是**纯模块**（无 LLM/loop/SDK 依赖，注入 `sleep`/`rng` 可单测）：`RetryPolicy`（enabled/max_attempts/base_delay_sec/max_delay_sec/multiplier/jitter）+ `is_retryable`（**provider 无关** duck-typing：先看 `status_code`/`status` 属性，再按异常类名匹配连接/超时类——**不 import anthropic/openai**；可重试 408/409/429/500/502/503/504/529 + 其余 5xx 兜底 + 连接/超时类名，不可重试 400/401/403/404/413/422 + 未知异常保守 fail-fast；非 Exception 如 KeyboardInterrupt 永不重试）+ `compute_delay`（`min(max_delay, base_delay × multiplier^(attempt-1))` + 可选 full jitter `uniform(0, delay)`）+ `run_with_retry`（驱动：耗尽后上抛**最后一次**原异常，`on_retry` 回调供 observability）。**驱动点**：`loop.py:_invoke_provider` 把整次 provider 调用（含流式消费，D5）包进 `run_with_retry`——重试包整层而非 mid-stream 续传，故流式重试会**重启 StreamPrinter 重打印已打印的局部文本**（罕见，transient 错误绝大多数在首 token 前，已知限制）。`agent_loop` 加可选 `retry_policy` 参数（默认 None = 旧直抛行为，向后兼容）；重试耗尽后原异常仍落到 `except BaseException` → `raise LLMCallError(...) from exc`（契约不变）。`_StreamingUnavailableError`/`NotImplementedError` 流式回退是控制流信号、无 status_code、类名不在白名单 → `is_retryable` 天然返回 False，不被重试干扰。**app 层独占重试**：`AnthropicProvider`/`OpenAIProvider` 构造 client 时 `max_retries=0` 关掉 SDK 自带重试，避免 2×N 复合放大（`enabled=false` 即真正无重试，含 SDK）。**子代理继承（D6）**：`retry_policy` 透传 `get_handlers(subagent_retry_policy=)` → subagent lambda → `run_subagent` → `_run_subagent_sync` → 子 `agent_loop`（含后台子代理 + 嵌套子代理），让后台子代理也扛瞬时失败（注意 `hook_engine` 子代理**不**传，retry_policy 传）。配置见 `config.toml [retry]`（`_parse_retry_config` 逐字段容错不崩 boot，`enabled`/`max_attempts` 走 env 覆盖 `BAREAGENT_RETRY_ENABLED`/`BAREAGENT_RETRY_MAX_ATTEMPTS`，其余 config-only；`_build_retry_policy` 做 `RetryConfig`→`RetryPolicy` 转换）。关键文件：`src/core/retry.py`、`src/core/loop.py`（`_invoke_provider` + `agent_loop` 参数）、`src/provider/{anthropic,openai}.py`（`max_retries=0`）、`src/main.py`（`RetryConfig` + 解析 + 两处调用点注入）、`src/planning/subagent.py`（透传）。MVP 不解析 Retry-After、不做流式 mid-stream 续传/去重、不做熔断/令牌桶/跨会话重试预算（均为后续扩展位）。

### 提供商抽象 (`src/provider/`)
`BaseLLMProvider`（base.py）为抽象基类，`AnthropicProvider` 和 `OpenAIProvider` 为具体实现（OpenAI provider 也覆盖 DeepSeek 等 OpenAI 兼容端点）。`factory.py` 负责工厂创建。统一的 `LLMResponse` 包含工具调用、文本、思考过程、token 计数。支持流式（`create_stream()`）和非流式（`create()`）。

### Prompt 缓存 (`src/provider/`，ROADMAP 提效降本)
给反复重发的大块上下文打缓存断点，让多轮 agent loop 的输入成本走缓存读（约 0.1× 输入价）。`CacheConfig`（`base.py`，`enabled`/`ttl`，复刻 `ThinkingConfig` 的 config→dataclass→factory→provider 穿透范式）。**断点注入仅在 `AnthropicProvider._build_request_params`**（loop/provider 接口零改动）：缓存开启时给最后一个 tool 挂 `cache_control`、把 `system` 由 bare string 转成含 `cache_control` 的 text block 列表（Anthropic 要求 system 为 block 列表才能挂断点）、并由 `_apply_conversation_breakpoint` 给最近一条消息的最后一个**可缓存** block（text/image/tool_use/tool_result/document，跳过 thinking）挂一个**移动**断点（每请求只比上次多几个 block，20-block 回溯稳命中增量缓存）。断点数 tools(1)+system(1)+对话(1) ≤3，远小于 Anthropic 上限 4。`cache_control` 值 5m=`{"type":"ephemeral"}`、1h=`{"type":"ephemeral","ttl":"1h"}`。**`cache_config=None` 或 `enabled=false` ⇒ 缓存关闭、请求体与未接缓存前字节级一致**（裸 `AnthropicProvider(api_key, model)` 构造默认 None=关；`factory` 总是传 enabled 实例，故 app 默认 ON）。渲染顺序 tools→system→messages，BareAgent 的 system 会话内静态（memory recall 注入 user 消息末尾不污染前缀），天然适合缓存；Opus 4.5+ 最小可缓存前缀 4096 token，低于阈值静默 no-op（不报错）。**用量归一化（跨 provider）**：`LLMResponse` 加 `cache_creation_input_tokens`/`cache_read_input_tokens`，三字段语义统一为 `input_tokens=全价` / `cache_read=折扣读` / `cache_creation=写溢价`（**相加非重叠**）；各 provider 的 `_parse_response` 负责归一——Anthropic 直接透传 usage（流式从 `get_final_message().usage` 读），OpenAI `input=prompt_tokens-cached`、`cache_read=prompt_tokens_details.cached_tokens`，DeepSeek `cache_read=prompt_cache_hit_tokens`（`_extract_cached_tokens` 统一抽取，含 chat/responses/两条流式路径 + `_merge_streamed_responses_result` 保留）。OpenAI provider **不收** `cache_config`（自动缓存无旋钮，只读用量）。**计价**：`token_tracker.py` 的 `DEFAULT_CACHE_MULTIPLIERS`（家族前缀→`(read_mult, write_mult)`，复用 `_longest_prefix_match`）= claude(0.1,1.25)/gpt·o1·o3·o4(0.5,0)/deepseek(0.1,0)，未知家族回退 (0.1,1.25)；`estimate_cost`/`summary` 计入缓存读写，`/cost` 仅在有缓存活动时多显 `Cache: N read / M write` 行（无缓存时输出与原先一致）。配置 `[cache]`（`enabled` 默认 ON + env `BAREAGENT_CACHE_ENABLED`、`ttl` `5m`/`1h`，`_parse_cache_config` 逐字段容错），boot 固化随 provider 走配置热重载 **restart-required**（不进 hot 集）。MVP 不做 1h 写溢价精确 5m/1h 拆分计价（统一按 1.25× 近似）、不做更激进断点策略（动态阈值/thinking 缓存/per-tool 断点）、不做 bottom-toolbar 实时命中率、不做缓存开关热重载。关键文件：`src/provider/base.py`（`CacheConfig` + `LLMResponse` 缓存字段）、`src/provider/anthropic.py`（断点注入 + usage 解析）、`src/provider/openai.py`（`_extract_cached_tokens` 归一化）、`src/memory/token_tracker.py`（家族倍率 + 计价/展示）、`src/provider/factory.py`（`_build_cache_config` 穿透）、`src/main.py`（`[cache]` 解析 + Config 字段 + teammate factory 穿透）。配置见 `config.toml [cache]`。

### 工具系统 (`src/core/tools.py`)
工具以可调用对象注册在字典中。基础工具（`BASE_TOOLS`）：bash、read_file、write_file、edit_file、glob、grep、web_fetch、web_search。延迟加载工具（`DEFERRED_TOOLS`）：todo_*、task_*、subagent、load_skill、background_run、team_*。Schema 定义在 `core/schema.py`，处理器在 `core/handlers/`（含 `web_fetch.py`、`web_search.py`、`search_utils.py`）。`read_file` 支持多模态：按扩展名分派——图片（png/jpg/jpeg/gif/webp）→ base64 image 块（需 vision 模型）、PDF（.pdf）→ 按页提取文本（需 `[pdf]` extra，pypdf，lazy import，未装时返回友好提示而非崩溃，`pages` 参数选页范围如 "1-5"/"3"）、notebook（.ipynb）→ json 解析渲染 markdown/code cells + 输出、其余走 UTF-8 文本路径（offset/limit）。图片块复用 Anthropic 内部 shape，经 `loop.py:_tool_result` 的 `list[dict]` 直通通路（零改 loop/provider）。

### 权限模型 (`src/permission/guard.py`)
模式：DEFAULT（写操作需确认）、AUTO（安全模式自动批准）、PLAN（仅允许安全工具）、BYPASS（无检查）。内置危险模式检测（rm -rf、force push、DROP TABLE 等）。支持 allow/deny 规则（前缀匹配）。`clone()` 创建权限副本，`for_subagent()` 为子智能体创建隔离权限（模式级联 + fail-closed）。运行时可通过 `/default`、`/auto`、`/plan`、`/bypass`、`/mode` 命令或 `Shift+Tab` 快捷键切换权限模式。

### 多智能体协调 (`src/team/`)
`MessageBus`（基于 JSONL 的追加式邮箱）、`ProtocolFSM`（带轮询的请求-响应协议）、`AutonomousAgent`（守护进程式空闲-轮询-认领循环）、`TeammateManager`。协议：PLAN_APPROVAL、SHUTDOWN。

### 智能体类型系统 (`src/planning/agent_types.py`)
`AgentType` 冻结数据类定义子智能体配置（工具白/黑名单、max_turns、嵌套控制、权限模式覆盖）。内置四种类型：`general-purpose`（全量工具，可嵌套，200 轮）、`explore`（只读，50 轮）、`plan`（只读，50 轮）、`code-review`（只读，50 轮）。`resolve_agent_type()` 解析类型名称并回退到默认值。`filter_tools()` / `filter_handlers()` 按类型过滤工具和处理器。

### 子智能体委派 (`src/planning/subagent.py`)
隔离的消息上下文，递归深度限制（max_depth=3），基于 token 的消息压缩（50k 阈值）。支持 `agent_type` 参数选择智能体类型，`run_in_background` 参数后台异步执行。权限隔离：通过 `PermissionGuard.for_subagent()` 创建子级权限，后台智能体使用 fail-closed 模式。

### Git Worktree 子代理隔离 (`src/planning/worktree.py`)
`run_subagent(isolation="worktree")`（schema 暴露 `isolation: "none"|"worktree"`，LLM 可请求）让子代理在**独立的 git worktree + 临时分支**中工作，所有文件操作（bash/read/write/edit/glob/grep）落在隔离工作目录，不污染主工作区。对齐 ROADMAP 3.3 / Claude Code `Agent(isolation:"worktree")` 语义。`worktree.py` 是纯 git CLI 封装（无 LLM/loop 依赖，可单测），镜像 `context.py:_run_git_command` 的 subprocess 范式（utf-8 / errors=replace / timeout）：`is_git_repo`、`create_worktree`（worktree 落系统临时目录 `tempfile.mkdtemp(prefix="bareagent-wt-")`，分支 `bareagent/wt-<id>`，失败抛 `WorktreeError`）、`worktree_status`（`git status --porcelain` 非空即 dirty）、`remove_worktree`（`worktree remove --force` + `branch -D`，best-effort 幂等）。隔离核心是 `core/tools.py:rebind_workspace_handlers`——浅拷贝 handlers，只把 6 个文件 handler 的 partial 重绑到 worktree 路径（`write_file`/`edit_file` 从原 partial 的 `.keywords["diagnostics_hook"]` 取回复用），其余 handler 原样保留。生命周期全在 `_run_subagent_sync` 内（后台路径天然继承），重绑在 readonly-memory 包装之后、嵌套 subagent 闭包之前（确保 worktree 内再 spawn 的子代理也用 worktree 文件 handler，嵌套 isolation 默认 "none"，见 Out of Scope）；loop 结束后 `try/finally` 按 dirty 决定保留+回报路径/分支或自动清理，脚注追加到 result 尾部。**fail-open**：非 git 仓库 / worktree 创建失败 → 回退无隔离继续跑 + 脚注提示（隔离是便利层，安全边界仍是 PermissionGuard，与 hooks 一致）。worktree 的 git 命令不经 PermissionGuard（基础设施级，同 task.py/context.py）；子代理在 worktree 内的 bash/write 仍受 `child_permission` 约束。MVP 不加配置项（temp 前缀 / 分支名硬编码），不支持嵌套 worktree、自动 commit/merge/PR、worktree 内 LSP 重新 rooted（diagnostics 仍指向主 repo root）。关键文件：`src/planning/worktree.py`、`src/core/tools.py:rebind_workspace_handlers`、`src/planning/subagent.py`（`isolation` 参数贯穿 + `_finalize_worktree`）。

### 技能系统 (`src/planning/skills.py`)
从 `skills/*/SKILL.md` 自动发现技能。通过 `load_skill` 工具按需加载。当前技能：code-review、git、test。`SkillLoader` 支持**多扫描根**：仓库 `skills/`（checked-in 正典层）+ 可选 `generated_root`（经验式习得层，见下），两层都进开局列表 + 都可 `load_skill`，同名**正典优先**（先扫仓库，generated 重名跳过）。

### 经验式技能生成 (`src/planning/skill_gen.py` + `skill_store.py`，对标 Hermes Agent)
复杂的多轮任务收尾后，agent 自动把"这次怎么做成的"沉淀成可复用 SKILL.md 草稿，下次同类任务自动获得这份经验。对标 Nous Research **Hermes Agent** 的 `skill_manage` 自动生成 skill，但裁剪掉其全自主静默写 + 自进化的高风险部分，落到适合单人个人工具的**半自动**档：**自动起草 → 草稿区 → 用户提升**。**触发（决策3）**：`SkillGenerator`（纯逻辑、可单测，仿 `retry.py` 范式）维护跨 turn 累计计数器，`agent_loop` 每个 turn 自然收尾（stop、未失败/中断）时调 `skill_gen.note_turn(turn_tool_calls)`；**双条件 AND**（`should_draft_skill`：累计 `工具调用 ≥ min_tool_calls`(默认5) 且 `用户回复 ≥ min_user_replies`(默认3)）命中后由 REPL 触发**一次隔离的反思 LLM 调用**起草，随后 `reset()` 重置计数（一段多轮工作流打包成一个 skill）。**反思调用**（`main.py:_run_skill_reflection`）在**消息副本**上跑独立 `agent_loop`（`tools=[SKILL_CREATE_TOOL_SCHEMA]`、`skill_gen=None`、低 `max_iterations`），故真实会话历史/turn 返回值不被污染；模型可回 "no skill" 拒绝（低价值任务的第二道质量闸）。**`skill_create`（决策4，create-only）刻意不进全局工具集**，只在反思调用里暴露——天然实现"主循环触发、子代理拿不到、`auto_generate=false` 全链路短路"三件事（工具压根不存在）；入 `PermissionGuard.SAFE_TOOLS`（草稿写 `.pending/` 沙箱不弹确认）。**存储（决策2，`skill_store.py:SkillStore`）**：生成 skill 根 = `~/.bareagent/projects/<slug>/skills/`（复用 `derive_memory_slug`，项目隔离、不进版本控制，可配 `[skills] dir`），与仓库正典分离；草稿落 `.pending/<slug>/SKILL.md`，`/skill keep` 提升（挪到 live 根）、`/skill discard` 删除、pending 数量软上限 `max_pending`（默认10，按最旧裁剪，非时间 TTL）。生成 SKILL.md 沿用 BareAgent 轻格式（目录名=skill 名、首行=描述，匹配 `_extract_description`），**不引入** Hermes frontmatter（version/tags/category）。REPL 命令 `/skill`（list | keep <name> | discard <name>，never-raise）。`note_turn` 在 `/clear`·`/new` 重置。**MVP 不做自进化**（相似度匹配 + update 已有 skill，决策5 下次任务）、不做定时自评、不做自动提升、不做糊复杂度信号（撞错恢复/用户纠正）。配置见 `config.toml [skills]`（`auto_generate` 走 env `BAREAGENT_SKILLS_AUTO_GENERATE`，余 config-only，`_parse_skills_config` 逐字段容错；`_build_skillgen_config` 做 `SkillsConfig`→`SkillGenConfig` 转换），boot 固化随 provider 走 restart-required（不进 hot 集）。关键文件：`src/planning/skill_gen.py`、`src/planning/skill_store.py`、`src/planning/skills.py`（多根）、`src/core/handlers/skill.py`、`src/core/loop.py`（`skill_gen` 参数 + 计数）、`src/permission/guard.py`（SAFE_TOOLS）、`src/main.py`（`SkillsConfig` + 解析 + 构造 + 反思 + `/skill`）。

### 任务与 TODO 管理 (`src/planning/`)
`TaskManager`（tasks.py）：持久化 JSON 存储，状态（pending/in_progress/done/failed），依赖追踪。`TodoManager`（todo.py）：内存级会话作用域，优先级，提醒机制。

### 消息压缩 (`src/memory/compact.py`)
微压缩截断旧工具结果；完整压缩通过 LLM 生成摘要。基于阈值触发（50k tokens）。保留系统消息和近期上下文。

### 持久化记忆 (`src/memory/persistent.py`)
文件式跨会话记忆：一条记忆 = 一个带 frontmatter（name/description/metadata.type，type ∈ user/feedback/project/reference）的 `.md` 文件，`MEMORY.md` 作单行索引。`MemoryManager` 暴露六个文本编辑器式命令（`view`/`create`/`str_replace`/`insert`/`delete`/`rename`），契约对齐 Anthropic memory tool，但注册为**单个普通 client tool** `memory`（schema 在 `core/tools.py:MEMORY_TOOL_SCHEMAS`，handler `core/handlers/memory.py:run_memory` 薄封装委派给 manager），不绑定原生 `memory_20250818` 类型，故全 provider 通用。所有路径经 `core/sandbox.py:safe_path` 限制在记忆根内（兼容 strip `/memories/` 前缀），写入走 `fileutil.atomic_write_text`。存储位置可配置 `[memory] dir`，默认全局 `~/.bareagent/projects/<workspace-slug>/memory/`（slug 由 `derive_memory_slug` 派生）。会话开局 `MemoryManager.system_prompt_section()` 把 MEMORY.md 索引（前 `max_index_lines` 行）+ MEMORY PROTOCOL 注入 `assemble_system_prompt`。**召回层（recall layer，仿 Claude Code 相关性召回）**：`MemoryManager.recall(query, k)` / `recall_section(query, k)` 按 frontmatter `name + description`（缺失回退正文前 200 字）做跨语言词法匹配（ASCII 词 + 中文滑动 bigram），取 top-K 拼成 `<memory-recall>` 块；`main.py:_refresh_memory_recall`（仿 `_refresh_nag_reminder`，在 `_build_loop_compact` 的 `_compact` 里每轮 agent_loop 调用）剔除旧块并在最后一条真实 user 消息后注入新块，故 `/remember`、`/forget`、普通 user-turn 均自动获得逐轮召回注入，与开局索引注入互补。`recall_k`（`[memory] recall_k`，默认 5，0 = 关闭召回仅保留索引注入）控制条数。向量/语义召回仍是 `system_prompt_section()` 与 `recall()` 的后续升级位。权限：`memory` 入 `PermissionGuard.SAFE_TOOLS`（不弹确认，沙箱内 bookkeeping）。子代理只读隔离：`AgentType.memory_writable`（explore/plan/code-review 默认 False）——单工具无法按子命令名过滤，故由 `subagent.py:_make_readonly_memory_handler` 在子代理边界包装 handler、拒绝五个写命令、放行 `view`。REPL 命令：`/remember <文本>`、`/forget <文本>`（注入用户指令驱动 LLM 经工具落盘/删除并维护索引）。配置见 `config.toml [memory]`。

### 用户界面 (`src/ui/`)
`AgentConsole`（基于 rich 的输出）、`StreamPrinter`（流式输出）、`prompt.py`（基于 prompt-toolkit 的输入层）、`theme.py`（主题，默认 `catppuccin-mocha`）。后台通知通过 `concurrency/notification.py` 实现。

### 后台执行 (`src/concurrency/`)
`BackgroundManager`（background.py）：基于 threading 的后台任务管理，支持 submit/drain_notifications。`NotificationManager`（notification.py）：后台任务完成通知。`Scheduler`（scheduler.py，ROADMAP 4.1）：cron 风格定时任务调度——按固定间隔（秒）重复执行 shell 命令。`Scheduler` 只负责「定时 + 重复 arm」：每次 `_fire` 用 `threading.Timer`（daemon）触发后，把命令交给注入的 `BackgroundManager.submit`（用唯一 `loop-<id>-<run_count>` run_id 避开 submit 去重 `ValueError`）在后台线程执行，结果/失败经既有通知通道在下个 turn 浮现；fire 末尾重新 arm 下一个 Timer 实现自重排。`Scheduler` 自身**绝不碰 messages/console**（线程安全关键），`threading.Lock` 保护 job/timer 字典，`_fire` 整体包 try（Timer 线程异常不得逃逸）。`MIN_INTERVAL_SEC=5.0` 守护（防 `/loop 0` 打爆后台），低于报 `SchedulerError`。内存级（退出即清，无跨会话持久化）。REPL 命令 `/loop`：`/loop <秒> <命令...>` 创建、`/loop list` 列出、`/loop cancel <id>` 取消单个、`/loop clear` 清空、`/loop`（无参）= 列表 + 用法。**安全语义**：定时命令经 `run_bash` 但**不经 PermissionGuard 交互确认**（后台无人值守无法弹窗），与 `background_run` 同档（基础设施级）——`/loop` 创建路径与 `_HELP_TEXT` 均明示「runs WITHOUT permission prompts，请自行确保命令安全」。Scheduler 在 `_run_stdio_session` 于 `bg_manager` 之后实例化（runner=`partial(run_bash, cwd=workspace, raise_on_error=True)`），`finally` 里 `cancel_all()`（幂等）清理。MVP 不暴露给 LLM、不支持 cron 表达式 / one-shot 延时 / 次数上限，均为后续扩展位。

### 会话管理
`TranscriptManager`（memory/transcript.py）：会话转录持久化。REPL 支持 `/sessions` 列出历史会话、`/resume` 恢复会话、`/new` 开始新会话、`/clear` 清屏并重置。每个会话有唯一 ID（时间戳格式）。**对话导入导出（`src/memory/conversation_io.py`，ROADMAP 会话便携）**：纯模块（无 REPL/UI/loop 依赖，可单测）暴露 `render_markdown`（人读 markdown：跳过 system、user/assistant 文本、tool_use→单行 `- **Tool call** ``name``: ``input 预览``` 摘要、tool_result→截断代码块、thinking 默认不输出；复刻 `_replay_stdio_transcript` 遍历结构含 `tool_name_by_id` 关联）+ `to_export_json`（自包含 wrapper `{version,session_id,exported_at,messages}`，messages 原样保真含 system/thinking/工具）+ `parse_import`（自动判形：整篇 `json.loads` 成功后 dict 含 `messages`→取之 / 裸 list→直接用，失败回退 jsonl 逐行解析；校验 list[dict] 且每条有 `role`，否则 `raise ValueError(可读原因)`）。REPL 命令：`/export [markdown|md|json] [path]`（markdown 默认，落 `.transcripts/exports/<session>_<ts>.{md,json}` 或显式路径；`_dispatch_export_command` 用 `atomic_write_text` 落盘、整体 try/except never-raise、不经 PermissionGuard 同 /loop 档）；`/import <path>`（读 `.json`/`.jsonl` → `parse_import` 校验 → **载入新会话**：inline **镜像 /resume** 机制——`messages[:]=imported`、`token_tracker.reset()`、新 session id、`_set_compact_session_id`/`_set_interaction_logger_session`、`_switch_session_mailbox`、`spawned_agents={}`、`_build_handlers(runtime_id=new_sid)`、`_replay_stdio_transcript`、`_save_transcript_snapshot`；坏文件/坏 JSON/无 role 在 mutate 前全 `continue`，**fail-safe 零状态改动不崩**）。MVP 不支持「追加到当前对话」、不导出剪贴板/HTML、不批处理多会话（均后续扩展位）。关键文件：`src/memory/conversation_io.py`、`src/main.py`（`_dispatch_export_command` + `/import` inline 镜像 /resume + 命令登记）、`tests/test_conversation_io.py`。

### Token 用量与成本 (`src/memory/token_tracker.py`)
`TokenTracker`：进程级累计 LLM token 用量（`total_input`/`total_output`/`call_count` + 按 model 细分），`record(response, model)` 在 `agent_loop`（`loop.py`）每次 LLM 响应后调用（流式+非流式单点覆盖，可选 `token_tracker` 参数）。REPL `/cost` 命令展示当前会话累计：**总是**显 token 计数 + per-model 细分，有定价的 model 额外显 $ 估算，无价的标 `(no price)`。定价为**混合策略**：内置 Claude Opus/Sonnet/Haiku 4.x 参考价（`DEFAULT_PRICES`，前缀匹配，价格可能漂移），`[cost.prices."<model-id>"]`（单位每百万 token）可覆盖内置价或为任意 model 新增价；未知且未配价的 model 只显 token 不显 $。重置语义：`/new`·`/clear`·`/resume` 归零，`/compact` 不重置（同会话压缩）。配置见 `config.toml [cost]`。

### 追踪 (`src/tracing/`)
统一 Tracer 接口（`_api.py`）+ 代理（`_proxy.py`）+ 配置入口（`setup.py`）。后端：`JsonFileTracer`（始终启用，写入 `.logs/` 供 `/log` 与 web viewer 使用）、`LangfuseTracer`（设 `LANGFUSE_PUBLIC_KEY` 或 `[tracing] langfuse=true` 启用）、`OpenTelemetryTracer`（设 `OTEL_EXPORTER_OTLP_ENDPOINT` 或 `[tracing] opentelemetry=true` 启用）。多后端时通过 `CompositeTracer` 扇出。Langfuse/OTel 为可选依赖，需安装额外 extras。

### 调试与日志 (`src/debug/`)
`InteractionLogger`（interaction_log.py）：将完整 LLM 请求/响应 payload 按会话写入 `.logs/<session-id>/` 的 JSONL，支持订阅事件流。`DebugViewerHandler`（web_viewer.py + viewer.html）：内置只读 HTTP SPA，REPL 中通过 `/log` 命令启动（端口由 `[debug] viewer_port` 控制，默认 8321）。需在配置中将 `[debug] enabled` 设为 `true` 才会写日志。

### MCP 客户端 (`src/mcp/`)
将外部 [Model Context Protocol](https://modelcontextprotocol.io) server 作为可插拔工具源接入 BareAgent。`MCPManager`（manager.py）并发拉起所有 `[[mcp.servers]]`，每个 server 一个 `MCPClient`（client.py）+ `Transport`（transport/，ABC + stdio / http_legacy / http_streamable 三实现）。`registry.py` 把远端工具按 `mcp__<server>__<tool>` 命名注入 `get_tools()` / `get_handlers()`；声明 `resources` capability 的 server 额外得到 `mcp__<server>__resource_list` + `mcp__<server>__resource_read`；`prompts/list` 通过 REPL slash 命令 `/mcp:<server>:<prompt>` 触发。REPL 配套命令：`/mcp status|list|reload`。生命周期硬化：transport reader 线程主动感知 EOF / 断流 → manager 立刻标 UNHEALTHY 并通过 `BackgroundManager.notify` 推送通知；`atexit + SIGTERM` 兜底清理子进程；单次 tool result 在 registry 层按 `max_result_text_bytes` / `max_result_binary_bytes` 截断（256 KiB / 5 MiB 默认）以保护 LLM 上下文。子代理隔离：`AgentType.mcp_tools_enabled`（explore/plan/code-review 默认 False）。关键文件：`src/mcp/__init__.py`、`src/mcp/manager.py`、`src/mcp/registry.py`、`src/mcp/client.py`、`src/mcp/transport/`、`src/mcp/config.py`、`src/mcp/errors.py`。配置见 `config.toml [mcp]` + `[[mcp.servers]]`。

### LSP 客户端 (`src/lsp/`)
通过 [multilspy](https://github.com/microsoft/multilspy)（可选 extra：`uv pip install -e ".[lsp]"`）接入成熟 Language Server，让 LLM 拿到精确的符号导航 + 类型诊断。**multilspy 0.0.15 的语言到 server 映射**：Python → `jedi-language-server`（非 pyright；jedi 适合符号/导航，类型诊断弱）；TypeScript → `typescript-language-server`；Rust → `rust-analyzer`。`LanguageServerManager`（manager.py）按 `[[lsp.servers]]` 并发拉起所有 server，按文件扩展名路由；handshake 失败 / 超时标 UNHEALTHY 不阻塞 REPL boot。`tools.py` 注入四个只读 Tier-1 查询工具到 `DEFERRED_TOOLS`：`lsp_outline` / `lsp_definition` / `lsp_references` / `lsp_diagnostics`（坐标对 LLM 暴露 1-based，内部转 0-based 调 LSP）。**写工具 `semantic_rename(file, line, col, new_name)`**（引用感知的语义重命名，基于 `textDocument/rename`）**故意不带 `lsp_` 前缀**——`lsp_*`=只读查询、`semantic_rename`=写盘，读写边界干净。multilspy 0.0.15 的 `SyncLanguageServer` 无 rename 同步包装，`LanguageServerManager.request_rename` 走裸请求：`asyncio.run_coroutine_threadsafe(server.language_server.server.send.rename(params), sync_server.loop)` + `open_file` didOpen。`src/lsp/workspace_edit.py`（纯函数）解析 WorkspaceEdit 的 `changes` / `documentChanges` 两种形态、按 uri 分组、单文件内按位置倒序应用 TextEdit、`atomic_write_text` 落盘；资源型操作（CreateFile/RenameFile/DeleteFile）MVP 安全跳过并提示（不做文件级重命名）。语义：LSP 不可用 / 无路由 / 空编辑 → 显式 Error 不静默退化为文本替换（无 grep 回退、无 dry-run、无 prepareRename 预校验）。权限：`semantic_rename` 不入 `SAFE_TOOLS`，与 `write_file` 同档——DEFAULT 确认 / AUTO 通过 / PLAN 拒绝 / BYPASS 放行。子代理隔离：因不带 `lsp_` 前缀，加入 `agent_types._READ_ONLY_DEFAULTS["disallowed_tools"]`，explore/plan/code-review 拿不到该写工具。`diagnostics.py` 提供 Hybrid auto-diagnostics-on-edit 钩子（默认 OFF；`[lsp] auto_diagnostics_on_edit = true` 开启后，`edit_file` / `write_file` 成功后通过 diff 算法五元组 `(file, line, col, severity, message)` 计算新增诊断并追加 `Newly introduced diagnostics in <file>:` 段到 tool result）。生命周期硬化：multilspy 0.0.15 默认 `do_nothing` 吃掉所有 `publishDiagnostics`，manager handshake 后直接覆盖 `language_server.server.on_notification_handlers["textDocument/publishDiagnostics"]`，缓存到 `_ServerEntry.diagnostics`；watchdog 线程轮询 subprocess `returncode` 检测崩溃 → 标 UNHEALTHY + console 推送 + `BackgroundManager.notify(f"lsp:{language}", ...)`；`atexit` 注册 `close_all`（幂等，与 MCP 的 atexit 解耦共存）。REPL 命令：`/lsp status|list|reload <language>`。子代理隔离：`AgentType.lsp_tools_enabled`（4 个查询工具只读，explore/plan/code-review 默认 True）；写工具 `semantic_rename` 另由 `disallowed_tools` 黑名单隔离。关键文件：`src/lsp/{__init__,manager,tools,workspace_edit,config,diagnostics,coord,errors}.py`。配置见 `config.toml [lsp]` + `[[lsp.servers]]`（`semantic_rename` 无新增配置项，沿用现有 LSP 配置）。

### Hooks 系统 (`src/hooks/`)
工具调用前后的用户自定义 shell 钩子（ROADMAP 2.1）。用户在 `config.toml` 声明 `[[hooks]]`，BareAgent 在主循环工具执行的前后触发自定义命令。`events.py`：`HookEvent(StrEnum)` 仅两个事件 `PreToolUse` / `PostToolUse`（值对齐 Claude Code）。`config.py`：`HookEntry`（event / command / tool / timeout）+ `HooksConfig`（`matching(event, tool_name)` 按 event 精确 + tool 精确或 None 过滤，保序）+ `parse_hooks_config`（结构错误抛 `HookConfigError` 让 main.py 降级；单条非法 entry 跳过并记入 `skipped` 不整体崩）。`engine.py`：`HookEngine` 匹配 → 构造 JSON → 复用 bash.py 的跨平台 argv（Windows PowerShell + UTF-8 / 非 Windows `bash -lc`）→ `subprocess.run(..., input=json_str)`。**控制协议（exit code，对齐 Claude Code）**：PreToolUse exit 2 = 拦截（跳过 handler，stderr 作拒绝理由回灌 LLM 作 error result）/ exit 0 = 放行 / 其他非 0 = 非阻塞警告 + 放行；PostToolUse 退出码不改变工具结果（仅非 0 警告）。**失败模式 = fail-open**（PRD D3）：hook spawn 失败 / 超时 → 警告 + 放行，不挂主循环（PermissionGuard 才是安全边界，hooks 是便利层）。JSON stdin payload：PreToolUse `{event,tool_name,tool_input,session_id,cwd}`；PostToolUse 追加 `{tool_output,is_error}`（字段名对齐 Claude Code）。集成：`agent_loop` 加可选 `hook_engine` 参数（PreToolUse 插在权限通过后 / handler 前，PostToolUse 插在 handler 成功后 / `_tool_result` 前，handler 异常不触发 PostToolUse）；session_id 经 `_resolve_hook_session_id` 复用 `compact_fn.get_session_id`，cwd 用 `os.getcwd()`。**子代理不传 `hook_engine`（隔离，hooks 只在主循环触发）**。`engine.py` 不 import `src.core.loop`（避免循环依赖，engine 被 loop 调用）。MVP 不支持改写 tool_input、不支持 Stop/Notification 等其余事件、不支持 env 注入 / 热重载（均为后续扩展位）。配置见 `config.toml [[hooks]]`。关键文件：`src/hooks/{__init__,events,config,engine,errors}.py`、`src/core/loop.py`（Pre/Post 插入 + `hook_engine` 参数）、`src/main.py`（`HooksConfig` + Config 字段 + 解析 + 建 engine 传主循环）。

### 配置热重载 (`src/main.py`，ROADMAP 4.3)
改完 `config.toml` / `config.local.toml` 后无需重启即可应用**可热重载**子集，并清晰区分哪些改动**需重启**。REPL 命令 `/reload`：`load_config(config.path)` 重读 toml+local+env（**不重穿 CLI provider/model 覆盖**——provider 本就需重启）→ `_diff_config_for_reload(old, new)` diff 并分类 → 应用 hot 子集到运行时对象 → 打印摘要（已应用 / 需重启 / 无变更）。**hot 集**（`_HOT_RELOAD_PATHS` frozenset）= `ui.theme` + `permission.{mode,allow,deny}`，改完即生效（theme 经 `get_theme().switch()` + `ui_console.set_theme()`，permission 直接原地改 `PermissionGuard.{mode,allow_rules,deny_rules}`）；其余字段（provider / mcp / lsp / hooks / retry / cost / debug / tracing / memory / subagent / thinking 等 boot 时固化的连接/客户端/烤进对象）仅报告「需重启」、**不**改运行时。**diff/分类为 main.py 模块级纯函数**（`Config` 定义在 main.py，独立 core 模块会造 core→main 循环依赖）：`_diff_config_for_reload` 用 `dataclasses.asdict` 拍平成 `section.field` dotted leaf（`_flatten_config` 下钻一层，`permission.allow`/`cost.prices` 等 list/dict 整体作单叶子比较，顺序变也算变；跳过 `path` resolved 路径字段），`_HOT_RELOAD_PATHS` 命中进 `hot` 否则进 `restart`，返回 `ReloadReport(hot, restart)`（`ConfigChange(path, old, new)` 列表 + `changed` 属性）。**失败安全 = all-or-nothing**（D4）：`load_config` 抛错（坏 TOML / 校验失败）→ `_dispatch_reload_command` 捕获 `Exception` + 报错 +「保持当前配置」，**零应用**，不留半套状态、进程不崩。apply 时**逐项 apply 并把 live `config` 对应子段原地 mutate**（hot 字段同步到 live config 使后续读一致，restart-required 字段保留 live 旧值——「正在运行的配置」与报告一致）；非法 theme 名 / 非法 mode 字符串 → 该项跳过不崩、其余 hot 项仍应用。**被动 mtime 监听**（D1，无后台线程——后台线程改 console/permission 会重蹈 Scheduler 线程安全坑）：`_config_mtimes(config)` 纯函数 best-effort stat 主文件 `config.path` + `.local` 兄弟文件（缺失文件跳过），REPL 主循环每轮读输入前比对 `last_config_mtimes`，变化则打印一行「config changed on disk — type /reload to apply」并更新记录值（同一 mtime 不重复刷；session 启动时初始化一次避免首轮误报）；`/reload` 后也刷新 `last_config_mtimes`。命令登记：`_SLASH_COMMANDS` + `_HELP_TEXT` + dispatch if 链（`if text == "/reload"`）。MVP 不支持后台 auto-watch+apply（Out of Scope，线程安全）、不引 watchdog（被动 mtime 零依赖）、不热重载 provider/mcp/lsp/hooks（boot 固化）。关键文件：`src/main.py`（`_diff_config_for_reload` + `ReloadReport`/`ConfigChange` + `_dispatch_reload_command` + `_config_mtimes` + mtime 检查 + dispatch 分支）、`config.toml`、`tests/test_config_reload.py`。

## 配置

`config.toml`（默认值）→ `config.local.toml`（本地覆盖，已 git-ignore）→ 环境变量 / CLI 参数（优先级递增）。

关键环境变量：`BAREAGENT_CONFIG`、`BAREAGENT_PROVIDER`、`BAREAGENT_MODEL`、`BAREAGENT_API_KEY`、`BAREAGENT_API_KEY_ENV`、`BAREAGENT_BASE_URL`、`BAREAGENT_PERMISSION_MODE`、`BAREAGENT_UI_STREAM`、`BAREAGENT_UI_THEME`、`BAREAGENT_THINKING_MODE`、`BAREAGENT_THINKING_BUDGET_TOKENS`、`BAREAGENT_SKILLS_DIR`、`BAREAGENT_SUBAGENT_MAX_DEPTH`、`BAREAGENT_SUBAGENT_DEFAULT_TYPE`。追踪相关：`LANGFUSE_PUBLIC_KEY`、`OTEL_EXPORTER_OTLP_ENDPOINT`。

CLI 参数：`--provider`、`--model`、`--config`。

## 代码规范

- 优先使用 Python 3.12+ 特性和标准库
- 遵循 PEP 8，保持清晰的类型注解
- 提交信息遵循 Conventional Commits：`Fix:`、`Feat:`、`Refactor:`、`Test:`、`Docs:`
- 新增行为需在 `tests/` 中补充 pytest 测试
- 保持实现简洁，避免过度设计

详细工程约定见 `.trellis/spec/backend/`（trellis 自动注入到所有 sub-agent 上下文）。
