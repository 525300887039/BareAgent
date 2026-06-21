# Implement — 会话 fork 与树状分支

## 前置

- 从最新 `main` 切特性分支：`git checkout main && git pull && git checkout -b feat/session-fork`（单人低摩擦，但本特性走分支 + 本地闸绿后提交，可选开 PR 让 CI 跨平台/pyright 兜底）。
- 类型门已 standard 且阻塞；新代码必须过 `uv run pyright`（1.1.409 已 pin）。需 ignore 时只用 targeted `# pyright: ignore[<code>]` + reason。源码禁 emoji。

## 有序清单

### 1. 纯模块 `src/bareagent/memory/session_tree.py`
- [ ] dataclass `ForkPoint(number, cut, user_preview, assistant_preview)`（frozen+slots）。
- [ ] `_has_tool_use(content)`、`_text_preview(blocks/str, limit=60)` helper。
- [ ] `enumerate_fork_points(messages) -> list[ForkPoint]`：扫描、追踪 last real-user preview、产出无-tool_use assistant 点。
- [ ] `slice_for_fork_point(messages, number) -> list[dict]`：枚举命中 → `copy.deepcopy(messages[:cut])`；越界 `raise ValueError`。
- [ ] dataclass `ForkRecord(parent, fork_point, parent_len, created)`（frozen+slots）。
- [ ] `load_tree(tree_path) -> dict[str, ForkRecord]`：fail-open（缺失/坏 json/坏条目）。
- [ ] `record_fork(tree_path, child, record)`：load → set → `atomic_write_text` 原子写。
- [ ] `render_tree(sessions, tree, current) -> str`：森林 + ASCII 连接线 + `@ turn N` + `● current` + 环防护 visited。
- [ ] 模块 docstring 说明纯逻辑/可单测/fail-open 契约。

### 2. 测试 `tests/test_session_tree.py`
- [ ] `enumerate_fork_points`：多轮 tool 周期 / compaction 摘要轮 / 仅 system（空）/ 末轮带 tool_use 未完成 / 连续 text assistant。
- [ ] `slice_for_fork_point`：深拷贝独立性 / 每点 slice 不变量（末条 assistant 无 tool_use）/ 越界 ValueError。
- [ ] `load_tree`/`record_fork`：round-trip / 缺文件→{} / 坏 json→{} / 坏单条跳过 / 原子写落盘。
- [ ] `render_tree`：多层 fork 缩进 / 孤儿父当根 / 当前节点标记 / 空 / 成环不死循环。

### 3. main.py 接线
- [ ] import：`from bareagent.memory.session_tree import enumerate_fork_points, slice_for_fork_point, load_tree, record_fork, render_tree, ForkRecord`（+ `utc_timestamp_iso` 已可从 fileutil 取）。
- [ ] helper `_transcript_tree_path(transcript_mgr) -> Path`：`transcript_mgr.transcript_dir / ".tree.json"`。
- [ ] `/fork` dispatch 块（插在 `/resume` 块之后、`/export` 之前）：无参列点 / `<N>` 切分 + 镜像 `/import` session-switch + 谱系 best-effort 写 + 状态消息。**参照 design.md 数据流伪码逐行落地**；复用 `_build_handlers` 全部 kwargs（照抄 `/import` 的参数集）。
- [ ] `/tree` dispatch 块（紧随 `/fork`）：load sessions+tree+current → `render_tree` → print；整体 try/except never-raise。
- [ ] `_SLASH_COMMANDS` 加 `"/fork"`, `"/tree"`（放在 `/sessions`/`/resume` 附近）。
- [ ] `_HELP_TEXT` 加两行：`/fork` 说明（list | `<N>` branch from a turn boundary）、`/tree` 说明（show session tree）。

### 4. 文档
- [ ] `CLAUDE.md`「### 会话管理」末尾补一段：会话 fork/树状分支（纯模块 `memory/session_tree.py` + `/fork`/`/tree` 命令 + sidecar `.transcripts/.tree.json` 谱系 + Out of scope），作独立 Docs commit。

## 校验命令

```bash
# 单测（Windows 本机用 .test，别用裸 uv run pytest；别和 Write/Read 同批）
uv run pytest tests/test_session_tree.py -q

# 全套本地总闸（4 步：ruff check → format --check → pyright standard → pytest）
bash scripts/ci-check.sh
```

- 手动冒烟（可选）：启 `bareagent`，跑几轮 → `/fork`（看点列表）→ `/fork 1`（看切换 + 状态）→ `/tree`（看谱系标记）→ `/resume <root-id>`（验证导航）。

## 风险文件 / 回滚点

- **`src/bareagent/main.py`**（唯一改的现存文件，且巨大）：只新增 import / helper / 两个 dispatch 块 / 两处登记，**不动** `/import`/`/resume`/`/new`/`/compact` 既有块。改完先 `ruff format --check` + `pyright` 确认无破坏。回滚点：dispatch 块是自包含 `if … continue`，可整块删。
- **session-switch 序列易漏项**：必须逐项对齐 `/import`（4093-4143）——尤其 `message_bus`/`main_mailbox_cursor`/`spawned_agents`/`handlers` 的重新赋值与 `_install_*` 三件套。漏任一项会导致新分支的 mailbox/handler 仍指向父会话。校验：与 `/import` 块逐行 diff 对比。
- **深拷贝**：`slice_for_fork_point` 必须 `copy.deepcopy`，浅拷贝会让新分支与父共享 block dict（后续 mutate 互污染）。单测显式验证独立性。

## 提交前检查（task.py finish 前）

- [ ] `bash scripts/ci-check.sh` 全绿。
- [ ] `/fork`/`/tree` 手动冒烟通过（或至少纯模块单测全绿 + main.py 接线 review）。
- [ ] CLAUDE.md 已补段（Docs commit 独立）。
- [ ] Conventional Commits 大写前缀；多行中文 message 用 Write 文件 + `git commit -F`（别用 PowerShell here-string）。
