# Repo Map: 符号骨架 + 结构全景工具

## Goal

让 agent 不靠「盲读整文件 / 反复 grep」就能拿到代码库的**结构全景**——类/函数的签名骨架（不含实现体），按需取用。这是已完成任务「语义代码检索 code_search」（task 06-19）的姊妹件，当时明确拆出来缓做。互补关系：code_search 找「相关代码块」，Repo Map 给「结构全景」。对标 Aider repo map（tree-sitter 符号骨架 + PageRank 图排名 + token 预算二分）与 Continue.dev repo map（受 Aider 启发）。

## What I already know（已对真实代码核实）

- **code_search 范式是本特性的实现模板**：纯逻辑模块 `memory/code_index.py`（注入式可单测、fail-open）+ boot 门控（`main.py:_build_code_index` 返回 None 则 `get_tools()`/`get_handlers()` 压根不暴露工具，「配好才出现」仿 MCP/LSP）+ 复用 `memory/embedding.py`。`code_search` handler 薄封装、`SAFE_TOOLS`、对 explore/plan/code-review 只读子代理开放。
- **LSP 路线现成件（B1）**：`lsp/manager.py:get_server_for_file(path)` 按扩展名路由到「健康且 RUNNING」的 `SyncLanguageServer`，`server.request_document_symbols(relpath)` 拿单文件符号（`textDocument/documentSymbol`），`lsp/tools.py:_format_outline` 已能渲染成缩进符号树。**但三重门控**：multilspy 是可选 extra + 必须按语言在 `[[lsp.servers]]` 手配 + server 健康；且**一次一文件**（整库 map 要逐文件 LSP 往返 + didOpen，慢且脆）。默认安装无任何 LSP server 运行。
- **tree-sitter 路线（B2）**：always-works、离线、快、语言无关、可整库扫，但加 tree-sitter + 语法/查询包新依赖（进 optional extra，未装时 fail-open 友好提示）。Aider/Continue.dev 都走这条。
- **省 token 消费模型**：官方最佳实践 = JIT 按需加载、不要把大块上下文塞进 system prefix（破缓存 + Rules Token Tax）。故 repo_map 应是 LLM 按需调的 DEFERRED 工具。

## Decision (ADR-lite)

**[Q1] 符号来源 = tree-sitter（已定）**。Context：repo map 本质是「整库结构全景」，需快、离线、always-works；LSP documentSymbol 受 multilspy 可选 extra + 按语言手配 `[[lsp.servers]]` + server 健康三重门控，默认安装零 server 在跑、整库要逐文件往返 + didOpen，applicability 太窄。Decision：MVP 走 tree-sitter（`tree-sitter` + `tree-sitter-language-pack`，进新 optional extra `[repo-map]`，未装 fail-open 友好提示），**不做 LSP/tree-sitter 混合**（复杂度翻倍、LSP 腿覆盖差）。LSP 的单文件符号能力已由现成 `lsp_outline` 覆盖，两者定位不冲突（`lsp_outline`=单文件精确、`repo_map`=整库快速骨架）。Consequences：加 tree-sitter 系新依赖（Windows wheel 已证实可用、无需编译器），需 vendor Aider Apache-2.0 `.scm` 查询 + 自己写「签名提取」逻辑。

**[Q1b] MVP 语言范围 = python + javascript + rust + go + java（已定）**。全部用 Aider 现成 Apache-2.0 查询（零 authoring 风险）。架构做成 drop-in（一个 `.scm` + 一条扩展名映射 = 一门语言）。TypeScript 暂缓（无现成查询、TS 语法节点异于 JS、需手写+测）→ Out of Scope（后续 drop-in 补）。可选锦上添花：对未 vendor 语言用 TSL pack `get_tags_query(lang)` 运行时探测兜底（失败跳过，放最后定）。

**[Q2] 排序 = 做 Aider 式 PageRank（已定，一步到位）**。MVP 即把图排名 + token 预算贴合做进去，repo_map 返回按重要性排序的骨架。要求 tag 查询同时捕获 `@definition.*` 与 `@reference.*`（Aider 现成查询已含 references）。图模型：文件为节点、「referencer→definer」为边、按提及次数 × 标识符稀有度（TF-IDF 式）加权（仿 Aider）。**PageRank 实现 = 手写幂迭代（power iteration，约 30 行），不引 networkx**（契合本仓库「最小依赖 + 纯逻辑注入式可单测」一贯做法；可否决改用 networkx）。token 预算：`max_tokens` 旋钮 + 按 PageRank 序渲染 + 二分贴合（±15% 容差仿 Aider），token 计数用近似（chars/4 量级，不引 tiktoken）。

**[Q2b] PageRank 偏置 = 自动会话偏置（已定）**。「最近读/改的文件」自动灌进 personalization 向量。仓库无现成追踪器，故新建。**解耦设计**：纯 repo_map 模块仍只接注入的 `focus_files`/`focus_identifiers`（零 REPL 依赖、可单测）；"自动"由薄接线层喂——新增**会话作用域 `FileRecencyTracker`**（小巧纯类、可单测：`record(relpath)` move-to-end + 有界容量 + `recent(n)` + `clear()`），主循环 read/edit/write 成功后记录工作区相对路径，仿 `spawned_agents`/`subagent_registry` 生命周期（`/new`·`/clear`·`/resume`·`/import` 清空、`/compact` 保留），子代理不喂（传 None）。repo_map handler 读 tracker top-N 当 focus。**附赠**：schema 仍暴露可选 `focus` 参数（文件/标识符），与自动 focus 合并（自动打底 + LLM 显式补充/覆盖）。自动偏置 MVP 只做「最近文件」；自动抽取 LLM 提及标识符 → Out of Scope（走显式 `focus`）。

**[Q4] 缓存 = 做 content-hash 增量缓存（已定）**。复用 code_index `EmbeddingCache` 范式：按 `(relpath, content-hash)` 缓存每文件抽取结果（definitions/references/签名行），调用时只重解析变更/新增文件、prune 删除、identity（tree-sitter 版本 + 查询集版本）整体失效、原子写、懒构建。缓存落项目目录 `repo-map-cache.json`（与 `code-index.json` 分离）。PageRank/图/渲染仍每次重算（依赖每次变的 focus）。

**[Q5] 输出格式 + 签名深度 = Aider 风格真实签名行 + 行号（已定）**。按文件分组（文件按 PageRank 序），文件内列各定义的**真实签名行**（含参数/返回类型，剥实现体），嵌套用缩进表达（类方法缩进在类下，靠 tree-sitter 节点包含关系算），每符号带行号（方便 LLM 续跳 read_file/lsp_outline）。签名切取启发式：从 definition 节点起取到 body 开始符（Python `:`、C-likes/JS/Rust/Go `{`）或首行兜底；多行签名尽量补全。已知近似点：多行签名补全、各语言 body 终止符差异。

## Open Questions

（全部已收窄）

## Technical Approach

**工具表面**：`repo_map(path=".", focus=[], max_tokens=<config默认>)` —— `path` 限定渲染子树（PageRank 仍按全库图算全局重要性，渲染时按 `path` 前缀过滤）；`focus` 可选（文件/标识符）与自动最近文件 focus 合并；`max_tokens` 预算。**boot 门控**（仿 code_search）：仅当 tree-sitter 系可 import（extra 已装）**且** `[repo_map] enabled` 时才注册工具（无则不暴露死工具，import 失败 fail-open）。**权限**：入 `SAFE_TOOLS`（只读）；对 explore/plan/code-review 只读子代理开放（**不**进 `MAIN_LOOP_ONLY_TOOLS`、**不**进 `_READ_ONLY_DEFAULTS["disallowed_tools"]`，同 grep/code_search）。

**分层（关键：可单测性）**：
- **纯核心模块**（无 tree-sitter 依赖、合成 tag 数据可单测）：引用图构建（文件节点、referencer→definer 边、提及次数 × 标识符稀有度加权）+ 手写幂迭代 PageRank（含 personalization 向量）+ 文件级排序 + 签名渲染（分组/缩进/行号）+ token 预算二分贴合（±15% 容差、近似 token 计数）。
- **抽取层**（需 tree-sitter、guarded import、fail-open）：`tree_sitter_language_pack.get_parser/get_language` 解析 → `tree_sitter.Query` 跑 vendored `.scm` → 收 `@definition.*`/`@reference.*` + 切签名行。注入式（测试喂 fake extractor）。
- **缓存层**：复用 code_index `EmbeddingCache` 范式，按 `(relpath, content-hash)` 缓存每文件抽取结果，identity = tree-sitter 版本 + 查询集版本。
- **会话层**：`FileRecencyTracker`（纯类）+ 主循环 read/edit/write 接线 + 会话切换清空。

**排序模型（MVP 简化，对 Aider）**：文件级 PageRank（非 Aider 的 per-symbol 排名）；文件内定义按源码顺序；预算按文件粒度裁剪（末个文件可符号粒度裁）。recent 文件经 personalization 提权 → 优先入选。

**依赖**：新 optional extra `repo-map = ["tree-sitter>=0.25", "tree-sitter-language-pack>=1.9"]`；vendor Aider Apache-2.0 `.scm`（python/javascript/rust/go/java）到 `src/bareagent/memory/repo_map_queries/` + NOTICE 署名。

## Requirements

- **纯核心模块**（如 `src/bareagent/memory/repo_map.py`）：图构建 + 幂迭代 PageRank（personalization）+ 签名渲染 + 预算二分，注入式 extractor、零 tree-sitter 依赖、可单测、fail-open。
- **抽取层 + vendored 查询**：tree-sitter 解析 5 语言（py/js/rust/go/java）抽 definitions/references + 切真实签名行；guarded import；未装/解析失败/无查询语言 → 跳过该文件、整体 fail-open。
- **content-hash 增量缓存**：复用 `EmbeddingCache` 范式，per-file 抽取结果，prune 删除文件、identity 整体失效、原子写、懒构建；缓存落项目目录 `repo-map-cache.json`。
- **`FileRecencyTracker`**（会话作用域纯类）：主循环 read/edit/write 成功记录工作区相对路径；`/new`·`/clear`·`/resume`·`/import` 清空、`/compact` 保留；子代理不喂。
- **`repo_map` 工具**：schema（`path`/`focus`/`max_tokens`）；handler 薄封装（合并 auto+explicit focus、调 index、格式化、never-raise）；boot 门控注册；`SAFE_TOOLS`；子代理只读可见。
- **配置 `[repo_map]`**：`enabled`（默认 true + env `BAREAGENT_REPO_MAP_ENABLED`）、`max_tokens`（默认 1024）、`max_file_bytes`（默认 1MB）、`recent_files`（auto-focus 条数，默认如 5）；`_parse_repo_map_config` 逐字段容错；boot 固化 restart-required。
- 补 pytest；ruff 只 format 改动文件；CLAUDE.md 同步；不破坏现有 code_search / LSP / memory recall 行为。

## Acceptance Criteria

- [ ] 签名抽取：5 语言正确抽类/函数**真实签名行**（含参数、剥实现体）+ 嵌套缩进 + 行号；单测覆盖（含多行签名近似）。
- [ ] PageRank：图构建 + 幂迭代收敛 + personalization 偏置（focus/recent 文件提权）确定性排序；合成 tag 数据单测（不需 tree-sitter）。
- [ ] 预算：`max_tokens` 二分贴合（±15% 容差），超预算按文件序裁剪；单测。
- [ ] 缓存：第二次只重解析变更/新增文件（fake extractor 计调用次数验证增量）、prune 删除、identity 变更整体失效；单测。
- [ ] `FileRecencyTracker`：record/move-to-end/有界/recent(n)/clear；单测。会话切换清空、`/compact` 保留。
- [ ] 工具表面：`repo_map` boot 门控（无 extra 不暴露）、`SAFE_TOOLS`、explore 子代理可见可调；`path`/`focus`/`max_tokens` 生效；单测。
- [ ] fail-open：extra 未装 / 解析失败 / 空仓库 → 友好提示不崩；单测。
- [ ] 配置解析容错 + env 覆盖；现有 code_search / LSP / memory recall 行为字节级不变。

## Definition of Done

- 新增/更新 pytest 单测（纯逻辑层注入式可测；tree-sitter 相关测试在未装时优雅 skip）
- `ruff check` 改动文件干净
- 新依赖进 optional extra `[repo-map]`，未装 fail-open 友好提示
- vendored `.scm` 带 Apache-2.0 NOTICE 署名
- CLAUDE.md 架构小节同步 + `config.toml [repo_map]` 示例
- 不破坏现有 code_search / LSP / memory recall 行为

## Out of Scope (explicit)

- **TypeScript**（无现成 Aider 查询、TS 语法节点异于 JS）—— 后续 drop-in 补（author 一个 `typescript-tags.scm`）。
- **per-symbol PageRank 排名**（Aider 把 rank 分摊到每个符号）—— MVP 用文件级排名 + 文件内源码序。
- **自动抽取 LLM 提及的标识符**做偏置 —— 走显式 `focus` 参数。
- **TSL pack `get_tags_query` 运行时探测兜底**（未 vendor 语言的机会性覆盖）—— 锦上添花，后续可加。
- networkx / tiktoken 依赖 —— 手写幂迭代 + 近似 token 计数替代。
- 文件 watcher 实时增量（懒构建 + content-hash 增量已够）；向量/语义（这是 code_search 的活）；repo_map 自动注入 system prompt（破缓存，坚持按需工具）。

## Implementation Plan (commits)

- **Commit 1**：纯核心模块 `repo_map.py`（图构建 + 幂迭代 PageRank + personalization + 签名渲染 + 预算二分，注入式 extractor）+ 单测（合成 tag 数据，零 tree-sitter）。
- **Commit 2**：tree-sitter 抽取层 + vendored `.scm`（5 语言）+ 扩展名→语言映射 + content-hash 缓存（复用 `EmbeddingCache` 范式）+ guarded import / fail-open + 单测（未装优雅 skip）。
- **Commit 3**：`FileRecencyTracker` + `repo_map` handler + boot 门控（`_build_repo_map_index`）+ `[repo_map]` 配置 + 工具 schema/权限/子代理接线 + main.py 5 处会话切换接线 + 单测。
- **Commit 4**：`pyproject.toml` extra `[repo-map]` + `config.toml [repo_map]` 示例 + CLAUDE.md 架构小节 + NOTICE 署名。

## Technical Notes

- 复用文件：`src/bareagent/memory/code_index.py`（boot 门控/fail-open/缓存范式模板）、`src/bareagent/lsp/manager.py`（`get_server_for_file`/`request_document_symbols`）、`src/bareagent/lsp/tools.py`（`_format_outline`/`_render_tree`/`_format_symbol_flat`/`_symbol_kind_label`）、`src/bareagent/core/tools.py`（boot 门控注入范式）、`src/bareagent/core/handlers/search_utils.py`（`iter_search_files`）。
- 来源决策史：归档 PRD `.trellis/tasks/archive/2026-06/06-19-repo-map-lsp-documentsymbol-embedding/prd.md`（「核心认识：两个独立特性」表 + Out of Scope 把本特性拆出）。
- 调研来源：Aider repo map = tree-sitter 抽声明/签名 + networkx PageRank（chat 文件 ×50、提及标识符 ×10 偏置）+ `--map-tokens` 默认 1k 二分贴预算（15% 容差）。

## Research References

- [`research/tree-sitter-packaging.md`](research/tree-sitter-packaging.md) — tree-sitter 2026 打包生态（已完成，实时 PyPI/wheel/GitHub 核实 2026-06-20）

### 研究关键结论（喂 MVP 决策）

- **Windows 可行性已证实**：`tree-sitter` 0.25.2（MIT）+ `tree-sitter-language-pack` 1.9.1（MIT，306 语言）都有预编译 Windows wheel（TSL pack 是 abi3，单 wheel 覆盖 3.10–3.14），**装时不需 C 编译器**。
- **没有「装一个包白拿查询」的路**：`.scm` tag 查询谁都不在磁盘发。需 **vendor Aider 的 Apache-2.0 `*-tags.scm`**（python/javascript/rust/go/java/c/cpp/ruby/... 共 32 个，**但无 typescript**，只有 javascript）或试 TSL pack `get_tags_query(name)`（覆盖未文档化，运行时探测）。
- **签名提取是我们自己的逻辑**：`.scm` 只给 `@definition.*` 节点；「只取声明/签名行、剥实现体」要在捕获之上自己写。
- **推荐依赖集**：`repo-map = ["tree-sitter>=0.25", "tree-sitter-language-pack>=1.9"]`（仿现有 `[lsp]`/`[pdf]`/`[embeddings]` extra）+ vendor 小批 `.scm`（Apache-2.0 attribution）。不依赖 `grep-ast`（它不带查询、还多拉 `pathspec`，而我们已有 `iter_search_files`）。
