# Implement — pyright 类型检查进 CI

## 前置

- 从最新 main 切新分支（改 CI 任务，沿用近期 CI 系列「分支 PR 先验证、再 ff-merge main」流程）：
  ```bash
  git checkout main && git pull --ff-only
  git checkout -b 06-21-pyright-ci
  ```
- pyright 已装（探查时 `uv pip install pyright` 装得 1.1.409）。

## 有序清单

### A. 真修复（让 pyright 变绿）— 先做，便于即时 `uv run pyright` 验证

1. **`src/bareagent/main.py:2949-2952`**：删中间布尔 `valid_budget`，改内联 isinstance if-block 赋值 `effective_budget`（见 design）。
2. **`src/bareagent/memory/code_index.py`**：`_search` 方法首行（docstring 后）加 `assert self._embedder is not None`。
3. **`src/bareagent/memory/persistent.py`**：`_semantic_recall` 方法首行（docstring 后）加 `assert self._embedder is not None`。
4. **`src/bareagent/memory/repo_map.py:190-191`**：`if total_p > 0:` 分支体首行加 `assert personalization is not None`。
5. **`src/bareagent/memory/repo_map_extract.py:144`**：`name = (name_nodes[0].text or b"").decode("utf-8", "replace") if name_nodes else ""`。
6. 验证：`uv run pyright` → 期望 `0 errors`（warnings 允许）。

### B. 接线（接 pyright 进门）

7. **`pyproject.toml`** dev extra：在 `ruff==0.15.8` 后加 `pyright==1.1.409`，附简短注释（exact pin 理由，复用 ruff pin 注释范式）。
8. **`.github/workflows/ci.yml`**：`test` job 在「Lint + format check (ruff)」step 后加一个新 step：
   ```yaml
   - name: Type check (pyright)
     # Type results are platform-independent; run once (on Linux) only. Blocking:
     # errors fail the test job -> folds into needs.test.result -> notify covers it.
     if: runner.os == 'Linux'
     run: uv run pyright
   ```
9. **`scripts/ci-check.sh`**：在 `(2/3) ruff format --check` 之后、`(3/3) pytest` 之前插入 pyright，并把三处 echo 标号 `/3` → `/4`（共 4 步：ruff check → format --check → pyright → pytest）。

### C. 防回归 guard

10. **`tests/test_ci_visibility.py`** 新增（紧邻现有 ruff pin / format-check 断言，复用 `_read`/`_command_lines` helper）：
    - `test_ci_workflow_runs_pyright`：`assert "uv run pyright" in _read(".github/workflows/ci.yml")`。
    - `test_ci_check_script_runs_pyright`：`assert "uv run pyright" in _read("scripts/ci-check.sh")`。
    - `test_pyright_pinned_exact`：`assert "pyright==" in pyproject` 且 `assert "pyright>=" not in pyproject`。

### D. 验证

11. 本机跑测试（**Windows 用 `.venv\Scripts\python.exe -m pytest`,不用 `uv run pytest`;且别和 Write/Read 同批工具调用**）：
    - `.venv\Scripts\python.exe -m pytest tests/test_ci_visibility.py`（新 guard 绿）
    - `.venv\Scripts\python.exe -m pytest`（全套绿，确认真修复无回归）
12. `uv run pyright` 再确认 0 error。
13. 本地闸：`bash scripts/ci-check.sh`（应跑 4 步全过；注意 ci-check.sh 内部用 `uv run pytest`,在 bash 工具下跑——若 PowerShell stderr 误判,改在 bash 工具里跑此脚本）。

### E. 文档（独立 Docs commit）

14. **`CLAUDE.md`「## CI 可见性」**：加 pyright 段，对齐既有 (1)(2)(3)(4) 编号风格——可作 (5)「pyright 类型检查上 CI（task 06-21-pyright-ci）」，记：配了没执行的门、10 error 真修复全绿阻塞、Linux-gated 并入 needs.test.result、pin 1.1.409、guard 断言、回滚预案。同步「关键文件」「防回归 guard」「MVP 不做」列表。

### F. 提交 + PR + 验证 + 合并

15. Conventional Commits（大写前缀冒号,多行中文用 Write 文件 + `git commit -F`,别用 here-string；源码禁 emoji）。建议拆：
    - `Fix:` 4 个 src 真修复（消 pyright error）
    - `Feat(ci):` 或 `Chore(ci):` pyright 接线（pyproject pin + ci.yml + ci-check.sh + guard 测试）
    - `Docs:` CLAUDE.md 同步
16. 推分支 → 开 PR → 等 CI（含 ubuntu+windows test leg、socket、notify 不触发因非 main push）连续绿。
17. 连续绿后 ff-merge main（`git checkout main && git merge --ff-only 06-21-pyright-ci && git push`）。
18. finish-work。

## 风险点 / 回滚

- **风险**：CI 上 pyright 出现本机没有的 error（环境差异）或 node 下载 flake。
  - 缓解：design 已论证 CI error 集 ⊆ 本机集；分支 CI 跑一遍兜底（步骤 16）。
  - 回滚：CI step 加 `continue-on-error: true` 退非阻塞保 main（复刻 windows-latest 预案）+ 拆后续修；ci-check.sh pyright 行临时降级。
- **风险**：assert 在 `-O` 下被剥离。已核实调用方守卫是真实安全网（code_index:197 / persistent:353），assert 仅作 narrowing + 文档,无实际安全损失。
- 单条真修复独立,`git checkout <file>` 可回退。

## 验证命令汇总

```bash
uv run pyright                                      # 0 errors
.venv\Scripts\python.exe -m pytest                 # 全套绿（Windows，不用 uv run pytest）
bash scripts/ci-check.sh                            # 本地闸 4 步全过
```
