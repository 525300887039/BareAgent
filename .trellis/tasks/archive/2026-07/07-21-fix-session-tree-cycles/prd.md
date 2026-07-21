# 修复 session tree 环形数据渲染

## 目标

让 `render_tree()` 在 lineage sidecar 包含纯环、自环或“正常树 + 独立环”损坏数据时仍 fail-open：`sessions` 中的每个 session 都可见且恰好出现一次，同时保持正常森林的格式、顺序、标注和终止性。

## 前置条件

- 本任务是父任务 `07-21-session-tree-release-v0-2-0` 的第一个交付物。
- 用户审阅全部规划材料并给出实施确认后才可启动。
- 本任务完成全部验收、质量门和独立 commit 前，不得启动发布加固子任务。

## 功能需求

- `sessions` 非空时，sidecar 是否存在 root 都不能导致 session 被隐藏。
- 两节点纯环中的两个 session 全部渲染，且每个只出现一次。
- self-cycle session 可见且只出现一次，不发生无限递归。
- 正常树与独立环共存时，正常森林先按现有行为输出，环中 session 随后全部可见。
- `current` 位于环中时继续带有现有 `● current` 标记。
- fork point 继续沿用现有 `@ turn N` 标注逻辑。
- 正常 root、orphan、siblings、多层树的连接符、缩进和确定性顺序不变。
- root 遍历和损坏组件兜底遍历均以 `sessions` 原始顺序为确定性来源。
- 不修改 sidecar 数据，不尝试自动“修复”或持久化环关系。

## 测试与质量需求

- 更新现有纯环/自环的弱或错误预期，使其断言可见性、唯一性和终止性。
- 至少新增或强化以下回归场景：
  - 两节点纯环全部可见且各出现一次；
  - self-cycle 可见且不递归；
  - 正常树与独立环同时存在时全部可见；
  - current 位于环中时正确标记。
- 定向运行 `tests/test_session_tree.py`。
- 运行完整项目质量门：Ruff lint、Ruff format check、Pyright、默认 pytest、socket pytest、docs build。

## 验收标准

- [x] `render_tree()` 对纯环、自环和混合损坏数据输出所有 `sessions`。
- [x] 每个 session ID 恰好渲染一次，环回边不产生重复行。
- [x] 正常森林现有测试继续通过，输出格式和顺序无回归。
- [x] current 与 fork point 标注在环形数据上仍正确。
- [x] 递归在所有回归场景中终止。
- [x] session tree 定向测试通过。
- [x] 六项完整质量门全部通过并记录实际结果。
- [x] diff 只包含本任务范围，并形成独立 `Fix:` commit。
- [x] task 验收项、commit 元数据和 session journal 完整。

## 非目标

- 不改变 sidecar schema、写入策略或 `load_tree()` 的容错策略。
- 不增加 SCC/图修复框架、循环警告 UI 或新的输出标记。
- 不修改 release workflow、版本或发布文档。
