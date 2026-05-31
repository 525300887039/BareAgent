# Cron 定时任务调度与 /loop 命令

## Goal

让 BareAgent 支持「定时/按间隔重复执行任务」(ROADMAP 4.1):用户用 `/loop` 创建按固定间隔重复运行的命令(如定期 `gh run list` 查 CI、轮询部署),并能列出 / 取消这些定时任务。复用现有 `BackgroundManager` + 通知通道,让结果在 LLM turn 之间自动浮现给用户/智能体。

## What I already know

- **REPL 主循环阻塞在 `read_fn()`**(`main.py` 约 1846)。任何定时触发的 **agent_loop 不能从后台线程跑**——会与主线程争用 `messages`/provider/console。这是核心架构约束。
- **`BackgroundManager`**(`src/concurrency/background.py`)已能在 daemon 线程独立运行 callable,把结果 `{task_id,status,result/error}` 推入队列;`drain_notifications()` 在 REPL tick 时取出。
- **通知注入**(`src/concurrency/notification.py:inject_notifications`)把 drain 出的更新拼成 `<background-notifications>` system 消息插入 messages——定时任务结果天然可走这条路浮现。
- **REPL 命令分发**:`main.py` 约 1855–2108 一串 `if text == "/x"`,每个 `continue`。`_SLASH_COMMANDS` / `_HELP_TEXT`(676–719)登记命令与补全。新命令照此模式加。
- **shell 执行**:`run_bash(command, timeout, *, cwd, raise_on_error)`(`src/core/handlers/bash.py`),已处理 Windows GBK→UTF-8。`raise_on_error=True` 时非零退出抛 `RuntimeError`(可让 BackgroundManager 归类 failed)。
- 无现成 scheduler;`src/concurrency/` 仅 background+notification。

## Technical Approach(推荐方案)

### 新建 `src/concurrency/scheduler.py`
- `@dataclass ScheduledJob`:`job_id / interval_sec / command / created_at?(不用 time,见下)/ run_count`。
- `Scheduler` 类(线程安全,`threading.Lock`):
  - `add(interval_sec, command) -> ScheduledJob`:校验 interval ≥ 最小值;建 `threading.Timer(interval, _fire)` 并 start;登记。
  - `_fire(job_id)`:把命令交给注入的 runner 执行并浮现结果(见下),`run_count += 1`,然后**重新 arm** 一个新 Timer(若未取消)→ 实现「重复」。
  - `list() -> list[ScheduledJob]`、`cancel(job_id) -> bool`(cancel Timer + 注销)、`cancel_all()`(退出时清理,幂等)。
- **执行 + 浮现**:`_fire` 调 `bg_manager.submit(f"loop-{job_id}-{run_count}", runner, command)`,runner=`partial(run_bash, cwd=workspace, raise_on_error=True)`。命令在 BackgroundManager 线程跑,结果/失败经既有通知通道浮现。每次 fire 用唯一 run_id 避免 submit 去重冲突。
- Timer 用 daemon-ish(threading.Timer 是普通线程但很短命;cancel_all 兜底)。

### `main.py` REPL 集成 `/loop`
- `/loop`(无参)→ 显示当前任务列表 + 用法。
- `/loop <seconds> <command...>` → 创建(秒为间隔,余下整体作命令)。
- `/loop list` → 列出 job_id / 间隔 / run_count / 命令。
- `/loop cancel <job_id>` → 取消单个。
- `/loop clear` → 取消全部。
- 注册进 `_SLASH_COMMANDS` + `_HELP_TEXT`;Scheduler 实例在 REPL 启动时建、`finally` 里 `cancel_all()`。

## Decision (ADR-lite) — 已与用户确认(按推荐采纳)

**Context**: 4.1 调度有 4 个落地决策点 + 一条安全语义需拍板。
**Decision**:
1. **执行内容 = 定时跑 shell 命令**(`/loop 60 gh run list`),结果经 BackgroundManager 通知通道在下个 turn 浮现。契合 ROADMAP 原文「按间隔重复执行指定命令」,复用现有后台基础设施、零架构改动。定时跑 agent_loop 需重构阻塞式输入 → Out of Scope。
2. **持久化 = 内存级,退出即清**。跨会话恢复涉及落盘 + 启动重排 + 时钟对齐,复杂度高、收益有限;扩展位保留。
3. **最小间隔守护 = ≥ 5 秒**,低于报错。防 `/loop 0 ...` 打爆后台。
4. **MVP 不给 LLM 暴露调度工具**,仅 REPL `/loop` 命令。给 LLM 自主排定时是更大权限面,留后续。
5. **安全语义**:定时命令经 `run_bash` 但**不经 PermissionGuard 交互确认**(后台无人值守,无法弹窗),与 `background_run` 同档(基础设施级)。须在 `_HELP_TEXT` / CLAUDE.md 明示「`/loop` 命令不走权限确认,请自行确保命令安全」。
**Consequences**: 实现简洁、零新依赖;代价是定时任务不持久(重启丢失)、且后台命令绕过权限确认(已明示警示)。命令形态:`/loop <秒> <命令>` / `/loop list` / `/loop cancel <id>` / `/loop clear` / `/loop`(无参=列表+用法)。

## Acceptance Criteria(evolving)

- [ ] `Scheduler`:add/list/cancel/cancel_all + Timer 重复 arm,线程安全,有单测(用极短间隔 + fake runner/event 验证多次触发,不依赖真实 sleep 墙钟)。
- [ ] `/loop` 五种形态在 REPL 可用,结果经通知通道浮现。
- [ ] `cancel` 后 Timer 不再触发;`cancel_all` 退出清理幂等。
- [ ] 非法输入(间隔非数字 / 缺命令 / 取消不存在 id)给清晰错误,不崩。
- [ ] pytest 全绿 / ruff / pyright 0;不新增第三方依赖(threading + 标准库)。

## Definition of Done

- 测试覆盖调度生命周期(重复触发、取消、清理)+ 命令解析。
- Lint / typecheck 绿。
- CLAUDE.md 架构段补一节调度器;`_HELP_TEXT` 同步。

## Out of Scope(explicit)

- 定时跑 agent_loop(架构上需重构阻塞输入为轮询,留待后续)。
- cron 表达式(`* * * * *`)——MVP 只支持「每 N 秒」。
- 跨会话持久化 / 重启恢复。
- one-shot 延时任务(`at`)、按次数上限自动停止——可后续加。

## Technical Notes

- 复用 `BackgroundManager.submit` 让命令执行 + 结果浮现走既有通路,Scheduler 只管「定时 + 重复 arm」,不自己碰 messages/console(线程安全的关键)。
- `threading.Timer` 每次 fire 后新建下一个(自重排),cancel 即不再重排。
- 时间戳:测试避免真实墙钟;`time.monotonic` 仅用于展示(若需),核心逻辑用 Timer 回调次数验证。
- 权限:定时命令经 `run_bash` 但**不经 PermissionGuard 交互确认**(后台无人值守);属基础设施级,与 background_run 同档——需在 PRD/文档明示这一安全语义。
