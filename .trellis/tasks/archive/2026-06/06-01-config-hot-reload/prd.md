# ROADMAP 4.3 配置热重载：watch config.toml + /reload

## Goal

让用户改完 `config.toml` / `config.local.toml` 后无需重启进程即可应用**可热重载**配置（theme、permission），并清晰区分哪些改动**需要重启**（provider、mcp、lsp 等 boot 时固化的连接/客户端）；改动失败（坏 TOML）时安全保持当前配置不崩。

## What I already know（探查结论）

- **重读入口现成**：`load_config(config_path, *, provider_override, model_override) -> Config`（main.py:399）已封装 config.toml + config.local.toml 合并 + env 覆盖。`config.path` 持有 resolved 路径，`/reload` 直接 `load_config(config.path)` 即可重读。
- **运行时可变对象**（在 `_run_stdio_session` 作用域内可直接改）：
  - **permission**：`PermissionGuard` 的 `mode` / `allow_rules` / `deny_rules` 三属性可变——`/default` 等命令（main.py:2169-2171）已原地改 `permission.mode`，`_build_permission_guard`（:807）展示 allow/deny 装配。
  - **theme**：全局 theme 单例，`tm.switch(name)` + `ui_console.set_theme(tm)`（`/theme` 命令 main.py:2189-2191）即可运行时切换。
- **boot 时固化（需重启）**：provider（client 在构造时建）、thinking（烤进 provider）、mcp/lsp（server 在 `start_all` 拉起）、hooks（engine boot 建）、retry_policy（`_run_stdio_session` 内构造一次并捕获）、debug/tracing/memory/cost/subagent。
- **命令分发**：main.py if-chain（`/loop`/`/theme`/`/mcp` 等模式），`_SLASH_COMMANDS`（:758）+ `_HELP_TEXT`（:779）登记。
- **Config 可变**：`@dataclass(slots=True)` 非 frozen，子段字段可原地改。
- **线程安全前车之鉴**：Scheduler 刻意不碰 messages/console（后台线程不能安全改 console/permission）。→ 任何「监听变更」若用后台线程 auto-apply 会重蹈该问题；手动 `/reload` 在 REPL 主循环同步执行，改 permission/console 安全。

## Requirements

- `/reload` 命令：重读磁盘配置 → diff 当前 live config → 应用**可热重载**子集（theme + permission）到运行时对象 → 打印摘要（已应用 / 需重启 / 无变更）。
- 纯 diff/分类逻辑可单测：`_diff_config_for_reload(old, new) -> ReloadReport`，把变更字段归类 hot（已应用）vs restart-required（仅报告）。
- 失败安全：`load_config` 抛错（坏 TOML / 校验失败）→ 捕获 + 报错 + 「保持当前配置」，**零应用**（all-or-nothing，不留半套状态）。
- 「监听变更」：被动 mtime 检查——每轮 REPL prompt 前比对 config 文件 mtime，变化则打印一行「config 已变更，/reload 应用」提示（无后台线程，无新依赖，改动仍在主循环手动应用）。
- `_SLASH_COMMANDS` + `_HELP_TEXT` 登记 `/reload`。
- 配置文档：CLAUDE.md 记录机制 + hot/restart 分类。

## Acceptance Criteria

- [ ] `/reload` 改 theme → 立即生效（颜色变）；改 permission.mode/allow/deny → 立即生效（guard 反映新值）。
- [ ] `/reload` 改 provider/model/mcp 等 → 报告「需重启」且**不**误改运行时。
- [ ] 坏 TOML `/reload` → 报错 + 保持当前配置，进程不崩、状态不变。
- [ ] `_diff_config_for_reload` 单测：hot 字段分类正确、restart 字段分类正确、无变更返回空报告。
- [ ] 被动 mtime 检查：文件变更后下个 prompt 提示一次（同一 mtime 不重复刷）。
- [ ] pytest 全绿、ruff clean、pyright 0、无新依赖。

## Definition of Done

- 新行为有 pytest（diff 分类 + 失败安全 + mtime 检测逻辑）。
- lint / typecheck / 测试全绿。
- CLAUDE.md + config.toml 注释更新。

## Technical Approach（决策见下，待用户确认）

`/reload` 同步跑在 REPL 主循环（线程安全）。diff/分类为 main.py 模块级纯函数（Config 定义在 main.py，独立 core 模块会造 core→main 循环依赖），apply（改 permission/theme）在 `_dispatch_reload_command`。被动 mtime 提示在主循环读输入前。

## Decision (ADR-lite)

**Context**: 配置 boot 一次性加载，多数运行时对象在构造时固化；需区分可热改 vs 需重启，并避免后台线程改 console/permission 的线程安全坑。

**Decision**（用户已确认全部推荐）:
- **D1** `/reload` 手动命令（主循环同步、线程安全）+ 被动 mtime 提示（每轮 prompt 前比对 config 文件 mtime，变化打印一行 `/reload` 提示）。后台 auto-watch+apply Out of Scope。
- **D2** hot 集 = `ui.theme` + `permission.{mode,allow,deny}`；其余变更仅报告「需重启」。retry/cost 作后续 hot 扩展位。
- **D3** diff/分类为 main.py 模块级纯函数 `_diff_config_for_reload(old, new) -> ReloadReport`（避免 core→main 循环依赖），apply 在 `_dispatch_reload_command`。
- **D4** all-or-nothing 失败安全：`load_config` 抛错 → 报错 + 保持当前配置 + 零应用。
- **D5** `load_config(config.path)` 重读 toml+local+env；成功后原地更新 live config 的 hot 字段，restart-required 字段留旧值；CLI provider/model 覆盖不重穿。

**Consequences**: 无新依赖、无后台线程；热改仅 theme+permission（与 ROADMAP 一致）；坏配置不崩 REPL；被动 mtime 增一次 stat/prompt（廉价）。

## Out of Scope

- 后台线程 auto-watch + auto-apply（重蹈 Scheduler 线程安全问题：后台不能安全改 console/permission）。
- watchdog 等文件监听依赖（被动 mtime 零依赖足矣）。
- 热重载 provider / mcp / lsp / hooks（boot 固化，需重启；本期仅报告）。
- 把 retry / cost prices 纳入 hot 集（技术上可行的后续扩展位，本期按 ROADMAP 只做 theme + permission）。
- CLI `--provider/--model` 覆盖的 reload 重穿（provider 本就需重启）。

## Technical Notes

- 关键文件：`src/main.py`（`_dispatch_reload_command` + `_diff_config_for_reload` + `ReloadReport` + mtime 检查 + `_SLASH_COMMANDS`/`_HELP_TEXT` + dispatch 分支）、`config.toml`、`CLAUDE.md`、`tests/test_config_reload.py`(新)。
- reload 复用 `load_config(config.path)`（重读 toml+local+env）。Config 子段可原地 mutate。
