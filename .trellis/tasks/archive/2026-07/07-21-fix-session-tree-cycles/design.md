# 技术设计：fail-open 渲染损坏 session graph

## 当前问题

`_build_children()` 以 `sessions` 构造 parent-to-children 邻接表，只把“无已知 parent”的 session 放入 `roots`。纯环和 self-cycle 中每个节点都有已知 parent，因此 `roots` 为空；`render_tree()` 只从 `roots` 调用 `walk()`，导致整个连通分量不可见。

此外，当前 `walk()` 在检查 `visited` 之前就 append 输出。若只增加未访问节点的兜底遍历，两节点环会按 `a -> b -> a` 把 `a` 输出两次，然后才终止，违反“恰好一次”。

## 最小修复

保留 `_build_children()`、`annotate()` 和正常 roots 遍历，只调整 `render_tree()` 的遍历纪律：

1. `walk()` 入口先检查 `sid in visited`，已访问则立即返回，不输出。
2. 首次访问时先加入 `visited`，再生成当前行并遍历 children。
3. 正常 `roots` 遍历完成后，再按 `sessions` 原始顺序检查每个 sid；对尚未访问的 sid 以 synthetic root 形式调用 `walk()`。

伪代码：

```python
def walk(sid, ...):
    if sid in visited:
        return
    visited.add(sid)
    append_line(...)
    for child in children[sid]:
        walk(child, ...)

for sid in roots:
    walk(sid, is_root=True)
for sid in sessions:
    if sid not in visited:
        walk(sid, is_root=True)
```

## 行为与顺序

- 正常森林：第一次 roots pass 已访问所有合法节点，第二次 pass 是 no-op，所以输出字节级保持现状。
- 两节点环：以 `sessions` 中首个未访问节点作为该损坏组件的显示 root；沿 children 输出其他节点，回边被入口 guard 截断。
- self-cycle：synthetic root 输出一次，自 child 回边被 guard 截断。
- 正常树 + 独立环：roots pass 保持正常树顺序；fallback pass 再按 `sessions` 首次出现顺序输出环组件。
- 多个损坏环组件：每个组件由其在 `sessions` 中最早的未访问节点确定显示顺序。
- `annotate()` 不变；已知 parent 的损坏节点仍按现有规则显示 fork point，current 比较也保持不变。

## 终止性与复杂度

`visited` 在递归 child 之前写入，每个 session 最多进入有效处理一次。图的节点数为 `V`、sidecar 中落在已知 sessions 的边数为 `E`，时间复杂度 `O(V + E)`，额外空间 `O(V)`（不含既有递归栈）。环回边只产生常数次已访问检查，不会无限递归。

## 取舍

- 不做强连通分量分解：需求只要求可见、唯一、确定和终止；SCC 会增加代码和新的排序决策。
- 不删除或重写损坏 edge：render 是纯函数，fail-open 展示不应改变持久数据。
- 不增加 `[cycle]` 文案：会改变输出契约，且用户未要求诊断格式。

## 回归测试设计

- 用完整输出或 `splitlines()` + 精确计数断言 session ID 每个只出现一次，不能只断言返回类型。
- self-cycle 断言单行和 fork point。
- 混合场景断言正常树先出现、独立环随后出现，并检查所有 ID 唯一。
- 环中 current 断言仅目标行含 `● current`。
- 保留已有 flat、multi-level、orphan、siblings 测试作为正常格式保护。
