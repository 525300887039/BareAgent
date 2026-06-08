# 语义/向量记忆召回（embedding 召回 + 词法回退）

## Goal

把持久记忆的召回层从**词法匹配**（ASCII 词 + 中文 bigram 重叠）升级为**语义/向量召回**（embedding 余弦相似度），让"换了措辞但意思相近"的查询也能命中相关记忆。当前 `recall()` 的 docstring 已明说"a future vector backend would replace this method body without touching the tool surface"——本任务兑现它，并**对嵌入不可用时 fail-open 回退现有词法召回**。

## What I already know

- 召回入口：`src/memory/persistent.py:recall(query, k)`（全量重扫 `.md`，按 `name+description` 词法重叠打分，top-K `RecalledMemory(path, description, score)`）+ `recall_section(query, k)`（渲染 `<memory-recall>` 块）。
- 注入点：`main.py:_refresh_memory_recall`（每轮 agent_loop 经 `_build_loop_compact` 调 `recall_section`，剔旧块 + 注入新块到最后一条 user 消息后）。`recall_k`（`[memory] recall_k` 默认 5，0=关闭）。
- 词法工具：`_lexical_terms`（ASCII 词 + CJK 滑动 bigram）+ `_relevance`（共享词数）。**保留作回退**。
- 记忆工作集很小（个位到几十个 .md 文件）→ 暴力余弦足够，无需向量 DB。
- 可选 extra 范式：`[lsp]`/`[pdf]`/`[langfuse]`/`[otel]` 懒加载可选能力。`numpy` **当前不是依赖**（向量计算需要，进 extra）。
- 用户当前 provider 是 codex relay（`gpt-5.4-mini`，base_url `.../codex/v1`）——**多半不 serve `/embeddings`**，故 backend 选型直接决定本特性对当前配置是否可用；fail-open 回退词法是关键。

## Decision (ADR-lite，全部已定)

- **Q1 embedding backend → (c) 可插拔抽象**：召回内核注入 `embed(texts)->vectors`；两个 backend（openai `/embeddings` + 本地），配置选；嵌入不可用一律 fail-open 回退词法。
- **Q6 本地库 → fastembed**（`research/local-embedding-libs.md`）：ONNX 无 torch、~35-45MB、自带 numpy、默认 `BAAI/bge-small-en-v1.5`(384维)。`[embeddings] = ["fastembed>=0.8"]` extra。API `list(TextEmbedding(model).embed(texts))`。
- **Q2 存储+相似度 → 磁盘缓存 + numpy 暴力余弦**：记忆根下单个 JSON 缓存 `{relpath: {hash, vector}}`，无向量 DB（工作集仅几十文件）。
- **Q3 回退+混合 → fail-open 回退词法 + 纯语义排序**：可用时纯 embedding 余弦 top-K；不可用回退现有词法 `recall()`；不做词法+语义混合打分（MVP 从简，混合留后续）。
- **Q4 索引刷新 → 懒算**：recall 时 embed query + 缓存缺失/hash 变更的记忆，算完回写；无写时钩子（下次 recall 自然补）。
- **Q5 配置 `[memory]`**：`semantic_recall`（默认 **false** opt-in，保持词法行为字节级兼容）；`embedding_backend`（`openai`|`local`）；`embedding_model`（openai 默认 `text-embedding-3-small`、local 默认 `BAAI/bge-small-en-v1.5`）；openai 的 `embedding_base_url`/`embedding_api_key`（留空复用会话 provider）；backend 初始化/调用失败 → fail-open 回退词法 + 一次 warning。

## Requirements (evolving)

- `recall(query, k)` 在 embedding 可用时按语义相似度返回 top-K；不可用时**字节级回退**现有词法行为。
- 工具表面（`recall`/`recall_section`/`RecalledMemory`/注入点）尽量不变（docstring 承诺）。
- embedding 调用失败/无 key/无 dep/离线 → fail-open 回退词法，绝不崩、绝不阻塞 loop。
- 新增行为有 pytest 覆盖（注入假 embedder 测语义排序 + 回退路径）。

## Acceptance Criteria (evolving)

- [ ] 注入假 embedder：语义相近但词法不重叠的 query 能命中正确记忆（词法召回会漏的 case）。
- [ ] embedder 不可用（抛错/None）→ 回退词法 `recall()`，结果与今天一致。
- [ ] 配置关闭语义 → 纯词法，字节级兼容。
- [ ] embedding 缓存按 content-hash 失效（文件改了重算）。
- [ ] ruff 干净，全量测试绿。

## Definition of Done

- Tests added；Lint / 全量测试 green；CLAUDE.md memory 小节补注；config.toml + 文档；（若加 extra）pyproject `[embeddings]`。

## Out of Scope (explicit)

- 向量 DB（chromadb/faiss）——工作集小，暴力余弦足够。
- 多模态 embedding、重排序（rerank）模型。
- 给 system_prompt_section 的开局索引也做语义（仍是 MEMORY.md 索引注入，本任务只动 recall 层）。
- 跨记忆的语义聚类/去重。

## Technical Notes

- 关键文件：`src/memory/persistent.py`（recall 内核 + 回退）、可能新增 `src/memory/embedding.py`（embedder 抽象 + 缓存 + 余弦）、`src/main.py`（`[memory]` 配置穿透）、`pyproject.toml`（视 backend 决定是否加 extra）。
- fail-open 心智对齐 hooks/worktree/retry：语义是增强层，词法召回是兜底安全网。
