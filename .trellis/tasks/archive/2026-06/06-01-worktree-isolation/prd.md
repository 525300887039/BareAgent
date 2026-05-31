# Git Worktree 子代理隔离

## Goal

让子代理(`run_subagent`)可在一个**独立的 git worktree + 临时分支**中工作:其所有文件操作(bash/read/write/edit/glob/grep)落在隔离工作目录,不污染主工作区。任务结束后——有改动则保留 worktree+分支并回报路径/分支供用户后续处理,无改动则自动清理。对齐 ROADMAP 3.3,也对齐 Claude Code `Agent(isolation:"worktree")` 的语义。

## What I already know

- `run_subagent`(`src/planning/subagent.py`)接收**已绑定父 workspace 的 handlers**(在 `tools.py:get_handlers` 用 `partial(run_bash, cwd=workspace)` / `partial(run_read, workspace=workspace)` 等绑定)。隔离的核心是「在 worktree 路径上重新绑定这 6 个文件操作 handler」。
- 文件操作 handler 共 6 个键:`bash`(cwd)、`read_file`/`write_file`/`edit_file`/`glob`/`grep`(workspace)。`write_file`/`edit_file` 的 partial 还带 `diagnostics_hook` 关键字,可从既有 partial 的 `.keywords` 取出复用。
- 已有 git subprocess 范式:`src/core/context.py:_run_git_command`(cwd + capture + utf-8 + errors=replace + timeout)。worktree.py 可镜像此范式。
- `run_bash` 已处理 Windows PowerShell GBK→UTF-8 对齐。worktree 的 git 命令走独立 subprocess(不经 bash 工具/权限),与 `task.py`、`context.py` 同档(基础设施级)。
- `generate_random_id(8)`(`fileutil.py`)可生成 worktree/分支后缀。
- subagent schema 当前暴露 `task` / `agent_type` / `run_in_background`;handler 闭包在两处(`tools.py:608` 主循环、`subagent.py:180` 嵌套)。

## Technical Approach(推荐方案)

### 组件:`src/planning/worktree.py`(无 LLM/loop 依赖,纯 git 封装,可单测)
- `is_git_repo(workspace) -> bool`:`git rev-parse --is-inside-work-tree`。
- `create_worktree(workspace) -> WorktreeHandle`:`git worktree add <path> -b <branch>`。path 用系统临时目录 `tempfile.mkdtemp(prefix="bareagent-wt-")`,branch=`bareagent/wt-<id>`。失败抛 `WorktreeError`。
- `worktree_status(path) -> (dirty: bool, summary: str)`:`git status --porcelain`,非空=dirty。
- `remove_worktree(handle)`:`git worktree remove --force <path>` + `git branch -D <branch>`(幂等,容错)。
- `WorktreeHandle` dataclass:`path / branch / base_workspace`。

### handler 重绑定:`tools.py:rebind_workspace_handlers(handlers, new_workspace) -> dict`
- 浅拷贝 handlers,只替换 6 个文件 handler 的 partial 指向 `new_workspace`;`write_file`/`edit_file` 从原 partial `.keywords` 取回 `diagnostics_hook` 复用。其余 handler(todo/task/skill/memory/mcp/lsp/subagent/web_*)原样保留。

### `run_subagent` 集成
- 新增参数 `isolation: str = "none"`(枚举 `"none"|"worktree"`),贯穿 `run_subagent` → `_run_subagent_sync`,以及两处 handler 闭包 + subagent schema(让 LLM 可请求)。
- `_run_subagent_sync` 内:isolation=="worktree" 且 `is_git_repo` → 建 worktree → `rebind_workspace_handlers(child_handlers, wt.path)` → 跑 loop → finally 里按 dirty 决定保留/清理 → 在返回结果尾部追加 `[worktree] ...` 脚注(路径+分支+是否保留)。
- 非 git 仓库 / worktree 创建失败 → **fail-open**:回退到无隔离 + 结果脚注提示(不让整个子代理失败),与 hooks 的 fail-open 同理(隔离是便利层,不是安全边界)。

## Decision (ADR-lite) — 已与用户确认(按推荐采纳)

**Context**: worktree 隔离有 4 个落地决策点需拍板。
**Decision**:
1. **落盘位置 = 系统临时目录** `tempfile.mkdtemp(prefix="bareagent-wt-")`。避免仓库内目录被子代理 glob/grep 扫到或误入 git 索引;git worktree 支持仓库外路径。
2. **dirty 判定 = 仅 `git status --porcelain` 非空**。子代理产出即未提交改动(自动 commit 已 Out of Scope),不引入 commits-ahead 比较。
3. **后台子代理同样支持 worktree**:生命周期全在 `_run_subagent_sync` 内,后台路径提交的正是此函数,天然继承,无特殊处理。
4. **MVP 不加配置项**,temp 前缀 / 分支名 `bareagent/wt-<id>` 全硬编码(项目规范「避免过度设计」);扩展位保留。
5. **fail-open**:非 git 仓库 / worktree 创建失败 → 回退无隔离继续跑 + 结果脚注提示,不让子代理整体失败(隔离是便利层,安全边界是 PermissionGuard,与 hooks 一致)。
**Consequences**: 实现简洁、零新依赖;代价是 worktree 散落系统 temp(用户需自行清理保留下来的 worktree——`git worktree list` 可见),且 dirty 判定不识别已 commit 的产出(MVP 不会发生)。

## Acceptance Criteria(evolving)

- [ ] `worktree.py` 四个函数 + dataclass,纯 git 封装,有单测(mock subprocess 或真实临时 repo)。
- [ ] `rebind_workspace_handlers` 正确重绑 6 个 handler 且保留 diagnostics_hook,有单测。
- [ ] `run_subagent(isolation="worktree")`:文件写入落在 worktree、主工作区不变;有改动保留+回报,无改动清理。
- [ ] subagent schema 暴露 `isolation`,LLM 可请求。
- [ ] 非 git 仓库 fail-open 回退,不崩。
- [ ] pytest 全绿 / ruff / pyright 0 error;不新增第三方依赖(git CLI + 标准库)。

## Definition of Done

- 测试覆盖核心路径(worktree 生命周期、重绑定、隔离落盘、fail-open)。
- Lint / typecheck 绿。
- CLAUDE.md 架构段补一节 worktree 隔离;config.toml 若加项则同步注释。

## Out of Scope(explicit)

- 嵌套 worktree(worktree 子代理再开 worktree 子代理)——共享父 worktree 即可。
- worktree 内自动 commit / merge / PR 创建——只保留分支供用户手动处理。
- 资源型 WorkspaceEdit、跨设备临时目录优化等边角。
- worktree 内 LSP 重新 rooted(diagnostics 仍指向主 repo root,MVP 容忍)。

## Technical Notes

- 镜像 `context.py:_run_git_command` 的 subprocess 范式(utf-8 / errors=replace / timeout)。
- Windows:`git worktree` 与路径分隔符均由 git 处理;tempfile 给绝对路径即可。
- 权限:worktree 的 git 命令不经 PermissionGuard(基础设施级,同 task.py)。子代理在 worktree 内的 bash/write 仍受其 `child_permission` 约束。
