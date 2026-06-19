# Repo Map + 语义代码检索

## Goal

让 agent 不靠「盲读整文件 / 反复 grep」就能理解代码库——通过 (a) 结构化符号骨架（repo map）和/或 (b) embedding 语义检索，按需取相关代码块，省 token + 提准。对标 Aider repo map（tree-sitter 符号骨架 + 图排名）与 Cursor 语义检索（embedding + grep 结合，实测准确率 +12.5%）。

## What I already know (已对真实代码核实)

- **`memory/embedding.py` 高度可复用**：`Embedder` Protocol + `OpenAIEmbedder`/`LocalEmbedder` + `build_embedder`(fail-open) + `cosine`(纯 Python) + `text_hash` + `EmbeddingCache`（`{relpath:(hash,vector)}` + identity 整体失效 + 内容 hash 单条失效 + `prune` + 原子写）。**语义代码检索 ≈ 复用这套 + 换 cache 位置 + embed 代码块**。
- **LSP 可拿符号但有门控**：`lsp/manager.py` 的 `request_document_symbols(relpath)`（`lsp/tools.py:_make_outline_handler` 在用，`textDocument/documentSymbol`）能编程式拿单文件符号骨架。**但 LSP 是可选 extra（multilspy）、按语言在 `[[lsp.servers]]` 配置 + 健康才可用、一次一文件**——整库 repo map 要逐文件 LSP 往返（慢 + 受配置门控），applicability 远窄于 tree-sitter。
- **现成范式可仿**：semantic recall 的懒算 + 缓存 + fail-open（`memory/persistent.py:_semantic_recall`）；工具注册走 `core/tools.py` 的 `DEFERRED_TOOLS`（延迟加载、不进开局 prefix）。
- **官方最佳实践（本会话调研）**：JIT / 按需加载、**不要把大块上下文塞进 system prefix**（破缓存 + Rules Token Tax）。故消费模型应是**LLM 按需调的工具**，不是自动注入 system prompt。

## 核心认识：这是两个独立特性

| 特性 | 实现路线 | 复用度 | 门控/代价 |
|---|---|---|---|
| **A. 语义代码检索** `code_search(query,k)` | 复用 `embedding.py` + 代码分块 + 索引 | **高**（基建已就位） | 需 embedder（openai key 或 local fastembed extra）；构建索引有一次性成本 |
| **B. Repo Map** `repo_map(paths?)` | (B1) LSP `documentSymbol` 复用 / (B2) tree-sitter 新依赖 | B1 中（受 LSP 门控）/ B2 低（新依赖） | B1 仅在 LSP 配好才全；B2 加 tree-sitter + 语法包依赖 |

## Decision (ADR-lite)

**Context**：repo map 与语义检索是两个独立特性、复用度/代价差大；消费模型与启用方式影响 token 与可用性。

**Decision**（用户已确认）：
- **MVP 只做 A（语义代码检索 `code_search`）；B（repo map）拆为单独后续任务。** A 复用度最高、语言无关、无门控/新重依赖、直接兑现「语义+grep」准确率红利。
- **消费模型 = 按需工具**（进 `DEFERRED_TOOLS`、不进开局 prefix），不自动注入 system（避免破缓存 + Rules Token Tax）。
- **分块 = 固定行窗**（默认 ~50 行/块 + ~10 行重叠），语言无关、零解析器依赖。
- **索引 = 首次调用懒构建**（仿 `_semantic_recall`），经缓存增量 embed + prune + cosine top-K；缓存独立落项目目录。
- **启用 = `[code_search] enabled` 默认 true，但工具仅在 boot 检测到可用 embedder 时才注册**（有则自动激活、无则静默不暴露，仿 MCP/LSP「配好才出现」），不做 opt-in false。
- **embedder = 复用 `[memory]` embedding 配置（backend/model/base_url/key），与 `semantic_recall` 开关解耦**；缓存文件独立。
- **复用 vs 新增**：复用 `embedding.py` 的 `Embedder`/`cosine`/`text_hash`/`build_embedder`/原子写/fail-open；新增薄纯模块 `code_index.py`（分块 + cosine top-K + chunk 级缓存，注入式 embedder 可单测），按 chunk-id（`relpath#startline`）存多块向量。

**Consequences**：
- 行窗分块牺牲符号边界对齐，换零依赖 + 语言无关；future 可由 repo map(tree-sitter/LSP) 升级成符号感知分块。
- 懒构建：首搜付一次性 embed 成本，后续增量；从不搜的用户零成本。
- boot 门控：无 embedder 时 LLM 看不到死工具；call-time embedder 报错仍 fail-open 返回友好提示引导 grep。

## Requirements

- **纯模块 `src/bareagent/core/code_index.py`**（或 `memory/`，落点依 spec）：固定行窗分块（`chunk_lines`/`overlap`，复用 `iter_search_files` + 1MB 上限 + utf-8 解码失败跳过）；注入式 `Embedder`；chunk 级缓存（key=`relpath#startline`、内容 hash 单条失效、identity 整体失效、prune 删除文件、原子写）；`cosine` top-K；全程 fail-open（embedder None/抛错 → 返回空或信号，绝不崩）。零 LLM/loop/REPL 依赖，注入 embedder 可单测。
- **`code_search` 工具**：schema（`query` 必填、`k` 默认 8、`path` 默认 `.`）；handler 薄封装调 code_index；进 `DEFERRED_TOOLS`；入 `PermissionGuard.SAFE_TOOLS`；对 explore/plan/code-review 只读子代理开放（同 grep，不进 `_READ_ONLY_DEFAULTS` 黑名单）。返回 top-K `file:start-end` + 片段；无 embedder/查不到 → 友好提示。
- **boot 接线（main.py）**：复用/抽出共享 embedder-build（从 `[memory]` embedding 配置，解耦 `semantic_recall`）；仅 embedder 可用时把 `code_search` 注入工具/handler 集；缓存路径走 `derive_memory_slug` 的项目目录、独立文件名（如 `code-index.json`）。
- **配置 `[code_search]`**：`enabled`（默认 true + env `BAREAGENT_CODE_SEARCH_ENABLED`）、`k`（默认 8）、`chunk_lines`（默认 50）、`chunk_overlap`（默认 10）、`max_file_bytes`（默认 1MB）；`_parse_*` 逐字段容错；boot 固化 restart-required。embedder 字段复用 `[memory]`。

## Acceptance Criteria

- [ ] `code_index` 分块：固定行窗 + 重叠正确切分；空文件/超限/解码失败文件被跳过；单测覆盖。
- [ ] `code_index` 检索：注入 fake embedder，cosine top-K 返回正确顺序的 chunk；`k` 生效；单测覆盖。
- [ ] `code_index` 缓存：第二次构建只 embed 变更/新增 chunk（fake embedder 记录调用次数验证增量）、删除文件被 prune、identity 变更整体失效；单测覆盖。
- [ ] `code_search` 工具：在 `DEFERRED_TOOLS` 与 `SAFE_TOOLS`；explore 子代理可见、可调；返回格式 `file:start-end` + 片段；单测覆盖工具表面。
- [ ] boot 门控：无可用 embedder 时 `code_search` 不进工具集（不暴露死工具）；有 embedder 时进；单测/main 测试覆盖。
- [ ] embedder 复用 `[memory]` 配置且不依赖 `semantic_recall` 开关；fail-open（embedder 抛错 → 友好提示不崩）。
- [ ] `[code_search]` 配置解析容错 + env 覆盖；现有 memory recall / LSP 行为不变。

## Definition of Done

- 新增/更新 pytest 单测（纯逻辑层注入式可测，仿 embedding/skill_gen 范式）
- `ruff check` 改动文件干净
- 新依赖（若 tree-sitter）进 optional extra，未装时 fail-open 友好提示而非崩
- CLAUDE.md 架构小节同步
- 不破坏现有 memory recall / LSP 行为

## Out of Scope (explicit)

- **Repo Map（B 特性，LSP/tree-sitter 符号骨架 + 图排名）** —— 拆为单独后续任务。
- 符号感知分块（需 tree-sitter/LSP/AST）—— MVP 用固定行窗；future 可由 repo map 升级。
- 向量数据库（工作集小，暴力余弦足够，同 semantic recall 决策）。
- 文件 watcher 实时增量（懒构建 + 内容 hash 增量已够）。
- rerank / 语义+词法混合打分（纯 top-K 起步；`score<=0` 排除）。
- boot 后台预建索引（懒构建，避免 boot 成本）。
- 独立 `[code_search]` embedder 配置（复用 `[memory]`）。

## Implementation Plan (commits)

- **Commit 1**：`code_index.py` 纯模块（分块 + cosine top-K + chunk 缓存，注入式）+ 单测。
- **Commit 2**：`code_search` schema/handler + `DEFERRED_TOOLS`/`SAFE_TOOLS`/子代理只读接线 + main.py boot 门控 + 共享 embedder-build 抽出 + `[code_search]` 配置 + 单测。
- **Commit 3**：`config.toml [code_search]` 示例 + CLAUDE.md 架构小节同步。

## Technical Notes

- 复用文件：`src/bareagent/memory/embedding.py`（Embedder/Cache/cosine）、`src/bareagent/lsp/manager.py`（`request_document_symbols`）、`src/bareagent/core/tools.py`（`DEFERRED_TOOLS` 注册）。
- 调研来源：本会话横向 code agent 简报（Aider repo map = tree-sitter + PageRank + `--map-tokens` 二分贴预算；Continue.dev repo map 受 Aider 启发；Cursor 语义+grep +12.5%）。
- 前置：本任务进实现前需先把 `feat/cache-abstraction-layer` 合 main 再从 main 开新分支。
