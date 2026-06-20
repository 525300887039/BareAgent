# Design — pyright 类型检查进 CI

## 架构与边界

本任务横跨「门的接线」与「让门变绿的真修复」两层，触及 7 个文件，无新增模块：

| 文件 | 改动 | 层 |
|---|---|---|
| `pyproject.toml` | dev extra 加 `pyright==1.1.409`（exact pin，紧邻 ruff pin，复用其注释范式） | 接线 |
| `.github/workflows/ci.yml` | `test` job 加 Linux-gated step「Type check (pyright)」跑 `uv run pyright` | 接线 |
| `scripts/ci-check.sh` | 加 `uv run pyright`（ruff 之后、pytest 之前；echo 标号 /3 → /4） | 接线 |
| `src/bareagent/main.py` | budget 解析内联 isinstance narrowing（消 2 error） | 真修复 |
| `src/bareagent/memory/code_index.py` | `_search` 首行 assert embedder（消 3 error） | 真修复 |
| `src/bareagent/memory/persistent.py` | `_semantic_recall` 首行 assert embedder（消 3 error） | 真修复 |
| `src/bareagent/memory/repo_map.py` | `if total_p>0` 分支内 assert personalization（消 1 error） | 真修复 |
| `src/bareagent/memory/repo_map_extract.py` | `Node.text` `or b""` 兜底（消 1 error） | 真修复 |
| `tests/test_ci_visibility.py` | 3 条静态 guard 断言 | 防回归 |
| `CLAUDE.md` | 「## CI 可见性」加 pyright 段 | 文档（独立 Docs commit） |

## 契约

- **pyright 通过判据**：`uv run pyright` exit 0。exit code 只由 **error** 决定；warning（未装可选依赖的 `reportMissingImports`）不影响。维持 `[tool.pyright]` 不动（basic / include=src/bareagent / reportMissingImports=warning）。
- **CI 阻塞语义**：pyright step 无 `continue-on-error` → error 致 `test` job 红 → 自动并入 `needs.test.result` → notify job 已覆盖（main 红开 ci-failure issue）。零额外接线（同 ruff/format-check 已验证的模式）。
- **本地闸忠实一致**：ci-check.sh 与 CI 跑同一 `uv run pyright`，pre-push 在 push 前即拦类型错。
- **guard 断言**：`tests/test_ci_visibility.py` 静态读文件断言 pyright 已接线（防回退到「配了没执行」态）。

## 10 个 error 的真修复（逐条，已核实调用方不变量）

1. **`main.py:2952`（×2 reportArgumentType）** `int(tool_budget)`，`tool_budget: Any|bool|int|None`。narrowing 藏在单独布尔 `valid_budget` 里，pyright 不跨变量传递。
   修：去掉中间布尔 + 内联守卫赋值（行为等价）：
   ```python
   if isinstance(tool_budget, int) and not isinstance(tool_budget, bool) and tool_budget > 0:
       effective_budget = tool_budget
   else:
       effective_budget = default_token_budget
   ```
2-4. **`code_index.py:215/233/241`（reportOptionalMemberAccess）** `self._embedder.identity`/`.embed`。`_embedder: Embedder|None`；`_search` 是私有方法，调用方 `search`（行 197 `if self._embedder is None: return []`）已守卫，但 narrowing 不跨方法。
   修：`_search` 首行 `assert self._embedder is not None`（1 assert 解 3 error，记录「调用方保证已设」不变量；`-O` 剥离无碍——调用方守卫是真实安全网）。
5-7. **`persistent.py:401/412/417`（reportOptionalMemberAccess）** 同上，调用方 `recall`（行 353 `if self._embedder is not None:`）守卫。
   修：`_semantic_recall` 首行 `assert self._embedder is not None`（1 assert 解 3 error）。
8. **`repo_map.py:191`（reportOptionalMemberAccess）** `personalization.get`，`personalization: dict|None`。`total_p>0`（行 190）只在 `if personalization:`（行 186）真时可达，但 pyright 不推。
   修：`if total_p > 0:` 分支内 `assert personalization is not None`。
9. **`repo_map_extract.py:144`（reportOptionalMemberAccess）** tree-sitter `Node.text` 是 `bytes|None`。
   修：`(name_nodes[0].text or b"").decode("utf-8", "replace")`（None → 空串，更安全）。

**全部行为保持或更安全**：无逻辑分支改变（int 重排等价、assert 在不变量满足时无操作、`or b""` 仅兜空）。

## 关键权衡 / 决策

- **assert vs 显式守卫**：选 assert——私有方法 + 调用方已守卫，assert 是 narrowing + 不变量文档的最简表达；显式 `if None: return` 会引入死代码（调用方已挡）。
- **pin 1.1.409 vs 最新 1.1.410**：pin 1.1.409（探查/triage 基线，可复现）。pip `pyright` wrapper 默认下载与 pip 版本相同的 node pyright，不自动升级（`PYRIGHT_PYTHON_FORCE_VERSION` 未设时；那条 "new version available" 仅是提示 nag），故 pin pip 版本即 pin node pyright 版本。升级留后续 deliberate bump（同 ruff 范式：reformat/re-triage 同 PR）。
- **Linux-only 跑**：类型检查平台无关（同 ruff），Linux 一次足够；不在 windows leg 跑（省时，结果一致）。
- **独立 step vs 合进 ruff step**：独立 Linux-gated step「Type check (pyright)」→ CI 日志中类型检查单独 green/red，可读性优于塞进 ruff step。仍在 test job、仍 Linux-gated、仍并入 needs.test.result。

## 兼容性 / 环境差异（重要）

- **CI error 集 ⊆ 本机 error 集**：CI 只装 `.[dev]`（无 repo-map/embeddings/lsp/tracing extra）。未解析的可选 import → 其类型为 Unknown → 抑制（而非制造）该模块相关的类型 error。本机装了 tree-sitter（repo-map extra），故 `repo_map_extract:144` 的 `Node.text` error 在本机出现、CI 可能不出现——本机是更严格环境。embedder（6）/budget（2）/pagerank（1）error 在 always-imported 代码，CI 同样出现。
  → **修完本机 10 个 error ⇒ CI 必 ≤ 0**。`repo_map_extract` 修复属防御性（CI 可能用不上但正确）。
- **分支 CI 兜底**：仍按近期 CI 系列流程，分支 PR 跑一遍 CI 确认 pyright 在 CI 实际 exit 0（捕获任何 CI/本机分歧），连续绿后 ff-merge main。
- pyright 仅入 dev extra：终端用户 `pip install bareagent-cli` 不受影响；CI/本地闸装 `.[dev]` 才有 pyright。

## 回滚

- 若 CI 上 pyright 意外失控（出现本机没有的 error / node 下载 flake 反复）：先给 CI step 加 `continue-on-error: true` 退非阻塞保 main 不红（复刻 windows-latest job 预案），把剩余修复拆后续任务；ci-check.sh 的 pyright 行可临时 `|| true` 或回退。
- 真修复均小且独立，单文件 `git checkout` 即可回退某条。

## 测试策略

- **不新增 runtime 测试**：10 个真修复是既有行为的类型硬化（非新行为），既有 1218 测试覆盖这些 runtime 路径；跑全套确认无回归即可（约定「新增行为补 pytest」——本任务新增行为是 CI 接线 + guard，已覆盖）。
- **新增 guard**：`tests/test_ci_visibility.py` 3 条静态断言（见 implement.md）。
