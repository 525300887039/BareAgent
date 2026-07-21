# 实施计划：session tree 环形数据

## 1. 启动与基线

- [x] 仅在用户实施确认后执行 `python ./.trellis/scripts/task.py start 07-21-fix-session-tree-cycles`。
- [x] 复核 `git status --short --branch`、`git rev-list --left-right --count HEAD...origin/main`，记录并排除未知用户改动。
- [x] 读取 PRD/design、backend quality/state-persistence spec。
- [x] 定向运行现有 `uv run pytest tests/test_session_tree.py`，记录基线。

## 2. 测试先行

- [x] 强化 `test_render_cycle_does_not_hang`：两个节点全部可见、每个一次。
- [x] 强化 `test_render_self_loop_does_not_hang`：self-cycle 单次可见且终止。
- [x] 新增正常树 + 独立环全部可见、顺序确定的测试。
- [x] 新增 current 位于环中的标记测试。
- [x] 如精确输出有助于保护 fork point/连接符，补充相应断言，但不扩大格式变更。
- [x] 确认新测试在未修复实现上因预期原因失败。

## 3. 最小实现

- [x] 在 `render_tree().walk()` 中把 visited guard 移到 append 之前，并在递归前标记首次访问。
- [x] roots 遍历结束后，按 `sessions` 原始顺序遍历所有尚未访问节点，作为 synthetic roots 输出。
- [x] 不改 `_build_children()`、sidecar schema、持久化或 annotation 逻辑。

## 4. 验证

- [x] `uv run pytest tests/test_session_tree.py`。
- [x] `uv run ruff check src tests`。
- [x] `uv run ruff format --check src tests`。
- [x] `uv run pyright`。
- [x] `uv run pytest`（记录 passed/deselected）。
- [x] `uv run pytest -m socket`（记录 passed）。
- [x] 在 `docs` 目录运行 `npm run docs:build`。
- [x] 检查 `git diff --check`、`git diff`、`git status --short`。

## 5. 提交与 Trellis 收尾

- [x] 评估是否需把“图渲染必须先判 visited 再输出”的非显然知识写入 backend state-persistence spec。
- [x] 按 Trellis Phase 3.4 展示一次提交分组；仅包含 session tree、测试及必要 spec。
- [x] 形成独立提交：`e43511f732de366cadabe8f9674a028ee013a281`（`Fix: 修复 session tree 环形数据渲染`）。
- [x] 记录 commit SHA 和验收结果，完成子任务归档/journal。
- [x] 只有以上全部完成后，返回父任务并启动发布子任务。

## 回滚点

- 新测试失败且暴露需求歧义：回到 planning 更新 PRD/design，不进入发布任务。
- 正常森林输出变化：撤回超出 visited/fallback 的实现，保留测试并收敛最小差异。
- 完整质量门失败：只修复本任务引入问题或报告现有基线差异，不放宽规则。

## 验证记录（2026-07-21）

- 变更前定向基线：`28 passed`。
- 测试先行：四个环形回归按预期失败（纯环/自环为空，混合场景缺少环组件）。
- 修复后定向：`30 passed`。
- Ruff lint：通过。
- Ruff format check：通过（先按固定 Ruff 格式化本次两个 Python 文件）。
- Pyright：`0 errors, 7 warnings`；warnings 均为既有可选依赖缺失。
- 默认 pytest：`1400 passed, 47 deselected`。
- socket pytest：`11 passed, 1436 deselected`。
- VitePress docs build：通过。首次误在仓库根目录调用 npm 因无 `package.json` 返回 ENOENT；随后在要求的 `docs/` 工作目录成功重跑。
