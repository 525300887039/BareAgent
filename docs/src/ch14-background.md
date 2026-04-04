# 后台执行

BareAgent 的后台执行能力实现于 `src/concurrency/background.py` 和 `src/concurrency/notification.py`。它的定位不是完整任务调度系统，而是一个很轻量的“守护线程 + 通知注入”机制。

最适合的心智模型是：

- 有些工作可以放到 daemon thread 里慢慢跑
- 主 agent 先继续往下走
- 等后续回合开始时，再把已完成结果注入消息历史

## 14.1 `BackgroundManager`

`BackgroundManager` 是后台执行的核心。它管理两类状态：

- 一个 `queue.Queue`，用于收集已完成任务的通知
- 一个 `_threads` 字典，记录当前按 `task_id` 索引的活动线程

### 线程模型

`submit(task_id, fn, *args)` 的行为是：

1. 先拿锁检查同名任务是否仍在运行
2. 如果还在运行，直接报错
3. 否则创建一个 daemon thread
4. 把这个线程记录到 `_threads`
5. 立即启动并返回 `task_id`

这里的 daemon thread 很重要。它意味着：

- REPL 退出时不会等待后台线程自然结束
- 后台执行的生命周期从属于当前进程

所以它并不适合做必须可靠落盘、必须跨进程恢复的任务。

### 同名任务去重

当前实现按 `task_id` 去重。如果你提交：

```python
submit("job-1", ...)
```

而 `job-1` 对应的线程还活着，会直接抛出：

```text
ValueError("Background task already running: job-1")
```

这使 `task_id` 同时承担了：

- 用户可读标识
- 运行中去重 key

### `_run()`

每个后台线程最终都会执行 `_run(task_id, fn, *args)`。它不会把异常向线程外传播，而是统一转成通知对象塞进 queue。

成功时写入：

```python
{
    "task_id": task_id,
    "status": "done",
    "result": result,
}
```

失败时写入：

```python
{
    "task_id": task_id,
    "status": "failed",
    "error": "RuntimeError: ...",
}
```

因此主循环消费后台任务时，不需要知道线程里发生了什么异常类型，它只需要读标准化通知。

### `drain_notifications()`

`drain_notifications()` 做两件事：

1. 把 queue 里当前所有通知一次性取空
2. 清理 `_threads` 中已经结束的线程记录

这意味着后台通知是“消费即移除”的，不会长期保留在内存队列里。

## 14.2 任务提交与通知

仓库里当前有两种主要方式会用到 `BackgroundManager`：

- `background_run` 工具
- `subagent(..., run_in_background=True)`

### `background_run`

`background_run` 的 runtime handler 由 `src/core/tools.py` 动态构造。它实际做的事情是：

1. 把同步 `bash` handler 包装成 `bash_runner`
2. 要求它在失败时抛异常，而不是返回错误字符串
3. 把这个 runner 交给 `BackgroundManager.submit()`
4. 立即返回提交结果

返回文本类似：

```text
Submitted background task job-1
```

这只是“提交成功”，不是命令执行成功。

### 和同步 `bash` 的差别

同步 `bash` 的错误会直接作为当前 tool result 返回给模型；后台 `background_run` 则不同：

- 当前回合只会看到“已提交”
- 真正的 shell 成功/失败结果会晚一点通过后台通知回流

`background_run` 复用了 `run_bash(..., raise_on_error=True)`，所以：

- 命令非零退出码时，后台通知会是 `failed`
- 错误文本里会带 `Command failed with exit code ...`
- 超时时也会被包装成失败通知

### 自动生成任务 id

如果调用 `background_run` 时没有显式提供 `task_id`，当前实现会自动生成一个 8 位随机字母数字 id。

### 不可用场景

如果当前环境没有绑定 `BackgroundManager`，`background_run` 不会尝试降级同步执行，而是直接返回：

```text
Background manager unavailable.
```

所以“后台能力可用”是显式运行时接入出来的，不是 schema 天生保证的。

### 后台子智能体

`subagent(run_in_background=True)` 的处理方式和 `background_run` 很像：

- 先生成一个 `subagent-xxxxxxxx` 的任务 id
- 再把 `_run_subagent_sync(...)` 丢进后台线程
- 当前回合只返回“已启动”

这意味着后台执行系统既能跑 shell，也能跑完整的子智能体任务。

## 14.3 `notification.py`：通知注入逻辑

大纲里把这一节叫作 `NotificationManager`，但当前仓库的真实实现并没有一个同名类。

现在负责“把后台结果送回消息历史”的，其实是 `src/concurrency/notification.py` 里的：

```python
inject_notifications(messages, bg_manager)
```

### 注入格式

`inject_notifications()` 会先调用：

```python
bg_manager.drain_notifications()
```

然后把本轮取到的所有通知包装成一条 system 消息：

```text
<background-notifications>
后台任务更新：
- Task job-1: done - ...
- Task job-2: failed - ...
</background-notifications>
```

成功通知会读取 `result`，失败通知会读取 `error`。两者都会被截断到最多 500 个字符，避免后台输出本身再次把上下文撑爆。

### 插入位置规则

通知不是永远直接 append 到末尾。当前实现的规则是：

- 如果末尾是普通 `role="user"` 消息，则把通知插到这条用户消息之前
- 如果末尾这条 `user` 消息本身是 `tool_result`，则直接追加到末尾
- 如果末尾不是 `user`，也直接追加到末尾

这样做的主要目的是避免把：

- “真实用户刚发出的请求”
- 和“后台任务稍后回来的状态”

写反顺序。

### 不是 UI 即时推送

这套机制还有一个很重要的边界：后台完成并不会立刻主动刷新终端 UI。通知只有在未来某次调用 `inject_notifications()` 时，才会真正进入消息历史。

所以它更准确地说是：

- opportunistic injection

而不是：

- out-of-band push

## 14.4 与 `agent_loop()` 的衔接

后台线程本身并不由 `agent_loop()` 启动，但 `agent_loop()` 负责在合适的时机把结果拉回来。

### `_run_background()`

主循环每轮开始时都会调用 `_run_background(bg_manager, messages)`。这一步本身不执行任何后台任务，它只做：

1. 从 `BackgroundManager` 提取完成通知
2. 用 `inject_notifications()` 把通知写回消息历史

因此在阅读这部分代码时，不要把：

- “提交后台任务”
- 和“消费后台结果”

看成同一件事。它们分布在完全不同的阶段。

### 什么时候模型能看到后台结果

后台结果被模型看见，取决于下一次 `agent_loop()` 何时开始新一轮。

常见情况有两种：

1. 如果 agent_loop 还在持续迭代，后台结果可能在后续轮次开始时被注入
2. 如果上一轮已经结束，后台结果通常要等到下一次用户再发消息，或下一次新的 loop 启动时才会被注入

这也是为什么 `background_run` 更适合“可以稍后再汇报”的任务，而不适合同步依赖结果的链路。

## 14.5 当前边界

BareAgent 的后台执行已经足够支持：

- 异步 shell 命令
- 后台子智能体
- 标准化 done/failed 通知
- 把结果重新送回消息历史

但它还没有做这些事：

- 后台任务持久化
- 进程重启后的恢复
- 并发度调度
- 取消正在运行的后台线程
- 独立的通知历史查询接口

所以最稳妥的定位仍然是：

- “为当前 REPL 进程提供轻量异步能力”

而不是：

- “完整后台作业平台”

## 小结

BareAgent 的后台执行由三部分拼起来：

1. `BackgroundManager` 负责把任务丢进 daemon thread，并收集完成通知
2. `background_run` / 后台 `subagent` 负责把具体工作提交进去
3. `inject_notifications()` 负责在未来回合开始时，把结果重新注入上下文

下一章会离开运行时机制，回到开发者视角：如果你要继续维护 BareAgent，本仓库的目录结构、开发命令、测试分布和扩展入口分别在哪些位置。
