# Design — 会话 fork 与树状分支

## 架构与边界

三块改动，严守 BareAgent「纯逻辑模块 + 注入回调可单测，副作用留 main.py」范式：

1. **新纯模块 `src/bareagent/memory/session_tree.py`**（零 LLM/loop/UI/main 依赖）：
   - fork 点枚举 + 合法切分（正确性内核）
   - 谱系 sidecar 读写（`.transcripts/.tree.json`）
   - 会话森林模型 + ASCII 渲染
   只依赖 stdlib（`json`/`copy`/`dataclasses`/`pathlib`）+ `core.fileutil.atomic_write_text`（与 transcript/persistent/embedding 同款原子写）。
2. **`src/bareagent/main.py` 接线**：两个 dispatch 块（`/fork`、`/tree`）+ `_SLASH_COMMANDS`/`_HELP_TEXT` 登记 + 一个小 helper `_transcript_tree_path`。`/fork` 复用既有 `/import` session-switch 序列与全部辅助函数；不改 `/import`/`/resume`/`/new`。
3. **`CLAUDE.md`「### 会话管理」**补一段说明（Docs commit）。

不新增配置项（sidecar 路径由 `transcript_mgr.transcript_dir` 派生）。不改 `transcript.py` 的 JSONL 格式（谱系走独立 sidecar）。

## 纯模块 API（`session_tree.py`）

### Fork 点枚举与切分（正确性内核）

```python
@dataclass(frozen=True, slots=True)
class ForkPoint:
    number: int            # 1-based，展示给用户的点编号
    cut: int               # slice 终点：messages[0:cut] 是 fork 前缀（深拷贝前）
    user_preview: str      # 触发该 turn 的最近「真实 user 提问」预览（截断 + 折行）
    assistant_preview: str # 该 assistant 回应的文本预览

def enumerate_fork_points(messages: list[dict]) -> list[ForkPoint]: ...
def slice_for_fork_point(messages: list[dict], number: int) -> list[dict]: ...
```

**合法边界判定（核心不变量）**：`messages[0:cut]` 合法 ⟺ `messages[cut-1]` 是 **assistant 且其 content 不含任何 `tool_use` 块**。论证：合法 agentic 会话内，唯一出现「无 tool_use 的 assistant」之处即 turn 末尾（中途 assistant 必带 tool_use 等 tool_result）；在此切，其之前每个 assistant 的 tool_use 都已在 [0:cut] 内配对了 tool_result → 前缀完整、role 交替、可接下一个 user turn。compaction 合成的 `assistant:"收到…"`（string content，无 tool_use）天然是合法点。

- `enumerate_fork_points`：顺序扫 messages；遇 `role=="user"` 且含 text 块（真实 user 轮，非纯 tool_result）→ 更新 `last_user_preview`；遇 `role=="assistant"` 且 `not _has_tool_use(content)` → 产出一个 `ForkPoint(number=自增, cut=i+1, user_preview=last_user_preview, assistant_preview=…)`。返回点列表（可能为空：仅 system、或唯一 turn 未完成）。
- `slice_for_fork_point`：枚举后按 `number` 命中 → `copy.deepcopy(messages[:cut])`（**深拷贝**，新分支与父不共享可变 block）。`number` 越界/无匹配 → `raise ValueError(可读原因)`（对齐 `parse_import` 的 ValueError 契约，main.py catch）。
- `_has_tool_use(content)`：`content` 为 list 时 `any(b.get("type")=="tool_use")`，否则 False。
- 预览 helper：取首个 text 块、`" ".join(split())` 折行、截断 ~60 字 + 省略号；空则回退占位（如 `(no text)`）。

### 谱系 sidecar

```python
@dataclass(frozen=True, slots=True)
class ForkRecord:
    parent: str
    fork_point: int        # 点编号 N（展示用）
    parent_len: int        # 父切片消息数 = ForkPoint.cut（展示用）
    created: str           # ISO8601，由 main.py 注入（模块不调 datetime.now，保纯）

def load_tree(tree_path: Path) -> dict[str, ForkRecord]: ...   # child_sid -> ForkRecord
def record_fork(tree_path: Path, child: str, record: ForkRecord) -> None: ...
```

- `load_tree`：文件不存在 → `{}`；`json`/schema 损坏（非 dict、字段缺失/类型错）→ `{}`（**fail-open**，逐条宽松：坏条目跳过而非整体丢，尽力保留好条目）。
- `record_fork`：模块级 `threading.Lock` 内 `load_tree` → 设 `tree[child]=record` → `atomic_write_json(tree_path, {sid: asdict(rec)})`（read-modify-write 结构化状态用 `atomic_write_json` + 锁，对齐 `state-persistence.md` 的 `TaskManager._save` 范式）。**best-effort**：写失败由 main.py try/except 吞（谱系是便利层，不阻断 fork 成功）。

### 森林模型 + 渲染

```python
def render_tree(sessions: list[str], tree: dict[str, ForkRecord], current: str | None) -> str: ...
```

- 节点 = `sessions`（= `TranscriptManager.list_sessions()`，已按新→旧排序）全集；边 = `tree` 中 parent→child。
- 根 = `tree` 里无记录的会话 **或** parent 不在 `sessions` 中的会话（孤儿当根，fail-open）。
- 递归渲染 ASCII：`├─`/`└─`/`│  ` 连接线；每节点行尾对 fork 子节点标 `@ turn N`（来自 ForkRecord.fork_point）；`current` 节点标 `● current`。
- **环防护**：渲染带 `visited: set`，已访问节点不再下钻（防 sidecar 被人为破坏成环时无限递归）。
- 根与 children 排序沿用 `sessions` 的新→旧序（稳定、可预测）。

## 数据流

### `/fork` 与 `/fork <N>`（main.py dispatch）

```
/fork（无参）:
  points = enumerate_fork_points(messages)
  points 空 → print_status("No fork points yet (need a completed assistant turn).")
  否则逐条 print: "<N>. user: <user_preview>  →  assistant: <assistant_preview>"
            末尾提示 "Use /fork <N> to branch."

/fork <N>:
  解析 N（非整数 → print_error 用法；continue，零状态改动）
  try: forked = slice_for_fork_point(messages, N)     # 深拷贝合法前缀
  except ValueError as e: print_error(e); continue     # 越界等，零状态改动
  parent_sid = _get_compact_session_id(compact_fn)      # 切换前捕获父 id
  _save_transcript_snapshot(transcript_mgr, messages, compact_fn)  # 确保父节点在盘（belt-and-suspenders）
  # —— 以下镜像 /import 的「新会话 + 立即切换」序列 ——
  messages[:] = forked
  token_tracker.reset()
  new_sid = _generate_session_id(transcript_mgr, reserved_ids={parent_sid})
  _set_compact_session_id(compact_fn, new_sid); _set_interaction_logger_session(...)
  message_bus, main_mailbox_cursor = _switch_session_mailbox(workspace_path, new_sid, current_bus=message_bus)
  spawned_agents = {}; pending_team_messages.clear(); pending_workflow_messages.clear()
  subagent_registry.clear(); workflow_registry.clear(); recency_tracker.clear()
  handlers = _build_handlers(... runtime_id=new_sid ...)
  _install_plan_handler / install_workflow_handler / install_subagent_send_handler
  _replay_stdio_transcript(messages, ui_console)
  _save_transcript_snapshot(transcript_mgr, messages, compact_fn)   # 落盘新分支
  # —— 谱系（best-effort）——
  cut = len(forked)
  try: record_fork(tree_path, new_sid, ForkRecord(parent_sid, N, cut, utc_timestamp_iso()))
  except Exception: ui_console.print_error("(lineage not recorded)")   # 不阻断
  print_status(f"Forked from {parent_sid} @ turn {N} into {new_sid} ({cut} messages).")
```

注意：`message_bus`/`main_mailbox_cursor`/`spawned_agents`/`handlers` 都是 REPL 局部变量，按 `/import` 同样方式重新赋值（闭包 helper `_drain_*` 等通过这些变量名读）。

### `/tree`（main.py dispatch）

```
try:
  sessions = transcript_mgr.list_sessions()
  tree = load_tree(tree_path)
  current = _get_compact_session_id(compact_fn)
  out = render_tree(sessions, tree, current)
  print(out or "No sessions.")
except Exception as e: print_error(...)   # never-raise
```

## sidecar 文件格式（`.transcripts/.tree.json`）

```json
{
  "20260621-101530-ab12cd": {"parent": "20260621-100000-xx99yy", "fork_point": 3, "parent_len": 7, "created": "2026-06-21T10:15:30Z"}
}
```

- 与 `<sid>_<ts>.jsonl` 同目录但以 `.` 前缀、固定名，不被 `*.jsonl` glob 命中（`list_sessions`/`_iter_entries` 只扫 `*.jsonl`）→ 零干扰既有扫描。
- 不进版本控制无需特殊处理（`.transcripts/` 已是运行时目录）。

## 兼容性

- JSONL 消息格式**零改动**；`/export`/`/import`/`/resume`/`/sessions`/`/new`/`/compact` 行为字节级不变。
- `.tree.json` 缺失时（旧仓库 / 从未 fork）`/tree` 自动退化为平铺会话列表（全是根），`load_tree → {}`。
- fork 的子会话就是普通 session（标准 `<sid>_<ts>.jsonl` 快照），可被 `/resume`/`/export` 等照常处理；即便 `.tree.json` 丢失，子会话本身不受影响（只是谱系信息没了）。

## 关键 Trade-offs

- **只 offer 干净 turn 边界（而非任意消息序号）**：牺牲「假装能在任意消息切」的细粒度（实际非 turn 边界都非法），换取「offer 的都是真合法点、无回退分支、单测干净」。已在 PRD 决策 A 拍板。
- **谱系走 sidecar 而非 jsonl 内嵌**：保 `/export`/`/import` 依赖的纯 JSONL 不被污染；代价是谱系与消息分两处存（sidecar 丢失 → 谱系丢失但会话仍在），可接受（fail-open）。
- **parent 显式预存快照**：理论上完成过 turn 的会话必已落盘（REPL 每 turn `_save_transcript_snapshot`，line 4363），fork 点存在 ⇒ ≥1 完成 turn ⇒ 父已在盘；仍加一次显式保存作 belt-and-suspenders（成本可忽略），保证 `/tree` 不出孤儿父。

## 回滚形状

- 纯加法：删除 `session_tree.py` + main.py 的两个 dispatch 块 + 登记行 + helper，即回到原状；`.tree.json` 残留无害（无人读）。
- 单 commit（feat）+ 单 Docs commit，便于整块 revert。

## 测试策略

- **纯模块单测**（重点）：
  - `enumerate_fork_points`：多轮含 tool_use/tool_result 周期、含 compaction 摘要轮、仅 system（空）、单轮未完成（assistant 带 tool_use 结尾 → 该点不 offer）、连续多 text assistant。
  - `slice_for_fork_point`：返回深拷贝（mutate 结果不影响原 messages）、每个 offer 点的 slice 满足「末条 assistant 无 tool_use」「无悬空 tool_use」、越界 N 抛 ValueError。
  - `load_tree`/`record_fork`：round-trip、缺失文件→{}、坏 json→{}、坏单条跳过保留好条目、原子写。
  - `render_tree`：多层 fork 缩进、孤儿父当根、当前节点标记、空 sessions、sidecar 成环不死循环。
- main.py 接线靠纯模块保证 + 手动冒烟（不强求 REPL 端到端测，沿用仓库惯例）。
