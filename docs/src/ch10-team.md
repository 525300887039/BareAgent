# 多智能体协调

BareAgent 的 team 系统不是一个抽象的“群聊”概念，而是一组可分解的基础组件：

- `TeammateManager` 负责持久化队友定义
- `MessageBus` 负责邮箱式消息传递
- `ProtocolFSM` 负责请求-响应协议封装
- `AutonomousAgent` 负责后台自治循环
- `/team` 命令负责把这些能力暴露给主 REPL

从实现上看，这套系统更接近“基于 JSONL 邮箱的轻量协作框架”，而不是复杂的分布式调度器。

## 10.1 消息总线

消息总线实现位于 `src/team/mailbox.py`，核心类型有两个：

- `Message`
- `MessageBus`

### `Message`

每条消息都包含以下字段：

| 字段 | 含义 |
|------|------|
| `id` | 消息 id；为空时发送前自动生成 |
| `from_agent` | 发送方 agent 名 |
| `to_agent` | 接收方 agent 名 |
| `content` | 文本内容 |
| `msg_type` | 消息类型，例如 `request`、`response`、`broadcast` |
| `timestamp` | ISO 8601 时间戳；为空时发送前自动生成 |
| `in_reply_to` | 可选，表示这是对哪条请求的响应 |

### 邮箱模型

`MessageBus` 采用“每个 agent 一个 JSONL 文件”的追加式邮箱模型：

```text
.mailbox/
  main.jsonl
  reviewer.jsonl
  tester.jsonl
```

每次 `send()` 都是在目标 agent 的邮箱文件末尾追加一行 JSON。

### session 级隔离

`MessageBus` 类本身默认目录是 `.mailbox`，但主 REPL 不直接使用这个默认值。当前 `src/main.py` 会为每个 session 单独创建：

```text
.mailbox/<session_id>/
```

因此在完整 BareAgent 运行时，实际邮箱路径更接近：

```text
.mailbox/20260405-120000-abc123/main.jsonl
```

这让不同 REPL 会话之间的队友消息天然隔离。

### agent 名约束

agent 名必须匹配：

```text
^[A-Za-z0-9_-]+$
```

也就是说只允许：

- 字母
- 数字
- `_`
- `-`

空字符串、空白名或带空格的名称都会被拒绝。

### `receive(since_id=...)` 的语义

`receive(agent_name, since_id=...)` 返回的是“游标之后”的消息：

- 如果 `since_id=None`，从头读完整个邮箱
- 如果 `since_id` 存在，则从该消息之后开始返回
- 游标对应的那条消息本身不会重复返回

这是一种“tail after cursor”的读取语义，适合持续轮询。

### 并发与等待

`MessageBus` 内部还维护了：

- 每个邮箱文件一把锁，保证追加和读取不会互相踩
- 条件变量和 signal 计数，用于 `wait_for_message()`
- 最近消息的内存索引缓存，用于 `find_message()`

因此 `ProtocolFSM.wait_response()` 不需要忙等轮询整个文件，而是可以在短轮询之外等待条件变量唤醒。

## 10.2 协议状态机

协议层实现位于 `src/team/protocols.py`。名字叫 `ProtocolFSM`，但当前实现更准确地说是一个“带协议封装的请求-响应 helper”，而不是复杂的显式状态机。

### 当前协议枚举

`Protocol` 枚举目前只定义了两个协议：

| 协议 | 用途 |
|------|------|
| `PLAN_APPROVAL` | 请求对方审阅计划并给出批准/拒绝意见 |
| `SHUTDOWN` | 通知对方停止运行 |

### 内容编码

协议消息会被编码成 JSON 字符串：

```json
{
  "protocol": "plan_approval",
  "content": "计划正文"
}
```

`decode_protocol_content()` 会尝试把普通文本解析回：

- `(Protocol, body)`，如果内容是合法协议包
- `(None, 原始文本)`，如果只是普通消息

### `request()` / `respond()` / `broadcast()`

`ProtocolFSM` 提供三类核心操作：

| 方法 | 作用 |
|------|------|
| `request(to, protocol, content)` | 发送一条 `msg_type="request"` 的协议消息 |
| `respond(in_reply_to, content)` | 对指定请求生成 `msg_type="response"` 的回包 |
| `broadcast(protocol, content)` | 向当前邮箱目录中除自己以外的所有 mailbox 广播 |

其中 `respond()` 的一个细节很重要：它会先查出原始请求消息，再复用原请求里的协议类型。也就是说，如果你响应的是 `PLAN_APPROVAL`，返回消息仍然会被包装成 `PLAN_APPROVAL` 协议内容，而不是纯文本。

### `wait_response()`

`wait_response(msg_id, timeout=60)` 的逻辑是：

1. 持续轮询当前 agent 的邮箱
2. 只关注 `msg_type == "response"` 的消息
3. 只有 `in_reply_to == msg_id` 才算目标响应
4. 每轮之间通过 `wait_for_message()` 等待最多 0.5 秒

因此它是“轮询 + 条件变量”的混合实现，而不是单次阻塞式 RPC。

## 10.3 自治智能体

长期运行的队友由 `src/team/autonomous.py` 中的 `AutonomousAgent` 表示。

### 核心循环

`AutonomousAgent.run()` 的顺序是：

1. 读取自上次游标之后的新邮箱消息
2. 如果有消息，优先处理消息
3. 如果没有消息且绑定了 `TaskManager`，再尝试认领 ready task
4. 如果两边都没有工作，则 `sleep(poll_interval)`

这意味着“邮箱消息优先于任务队列”。

### 处理邮箱消息

收到消息后，自治 agent 会按以下规则处理：

- 如果协议是 `SHUTDOWN`，立即设置 `_shutdown=True`
- 否则只有 `msg_type == "request"` 的消息会被当作工作请求
- 处理完后用 `ProtocolFSM.respond()` 给发送方回包

### `PLAN_APPROVAL` 的特殊提示

如果协议是 `PLAN_APPROVAL`，自治 agent 不会直接把原文转给 LLM，而是会先包一层提示：

```text
请审阅下面的计划，判断是否应批准，并给出简洁理由。
```

也就是说，当前 `PLAN_APPROVAL` 的“状态机”语义主要体现在 prompt 适配，而不是复杂的审批流控制。

### 任务认领

如果 agent 绑定了 `TaskManager`，它会调用 `get_ready_tasks()` 获取可执行任务，再通过：

```python
task_manager.update(task.id, status="in_progress", expected_status="pending")
```

进行乐观认领。

如果认领成功：

- 调用 `_run_prompt()` 执行任务
- 成功则把任务更新为 `done`
- 失败则标记为 `failed`

### 启动时忽略旧 shutdown

构造 `AutonomousAgent` 时，会把 `_last_seen_id` 初始化为当前邮箱里的最新消息 id。这样一来，启动前遗留在邮箱里的旧 `SHUTDOWN` 广播不会被重新消费，从而避免 agent 一启动就立刻停掉。

## 10.4 TeammateManager

`TeammateManager` 位于 `src/team/manager.py`，负责 teammate 定义的持久化，而不是消息通信本身。

### 持久化文件

主 REPL 默认把队友定义存放在工作区根目录：

```text
.team.json
```

这个文件是跨 session 共享的；和 session 级邮箱目录不同，它不会因为 `/new` 而切换。

### `Teammate`

每个 teammate 定义包含：

| 字段 | 含义 |
|------|------|
| `name` | 队友名称 |
| `role` | 简短角色描述 |
| `system_prompt` | 启动该队友时使用的系统提示 |
| `provider_config` | 可选 provider 覆盖配置 |

一个简化后的 `.team.json` 结构示例如下：

```json
{
  "teammates": {
    "code-reviewer": {
      "name": "code-reviewer",
      "role": "Review code for bugs and regressions",
      "system_prompt": "You are a code reviewer.",
      "provider_config": {
        "model": "gpt-4.1"
      }
    }
  }
}
```

### `spawn()` 的职责边界

`TeammateManager.spawn(name, provider_factory)` 只负责：

1. 读取并快照化队友定义
2. 用 `provider_factory` 构造独立 provider
3. 返回一个 `AgentInstance`

它不会自己启动线程，也不会自己加入消息总线。真正把队友跑起来，是主 REPL 在 `_make_team_handlers()` 里完成的。

## 10.5 `/team` 命令

对用户可见的入口是 REPL 命令：

```text
/team list
/team spawn <name>
/team send <name> <message>
```

### `/team list`

`/team list` 会列出当前 `.team.json` 中所有已注册 teammate，并附带一个 `running` 状态。

需要注意，这个 `running` 状态来自当前进程内的 `spawned_agents` 字典，而不是去扫描外部邮箱或后台线程。因此它表示的是：

- “这个 REPL 进程是否已经启动过该 teammate”

而不是：

- “系统里是否存在任何同名 agent 活动”

### `/team spawn <name>`

当前实现的启动流程是：

1. 通过 `TeammateManager.spawn()` 构造 `AgentInstance`
2. 为该 teammate 准备邮箱
3. 克隆一份 `permission`，并强制 `fail_closed=True`
4. 构造一套该 teammate 专用的 handlers
5. 创建 `AutonomousAgent`
6. 通过 `BackgroundManager.submit()` 以后台线程方式运行其 `run()` 循环

如果同名后台任务已在运行，会返回：

```text
Teammate <name> is already running.
```

### `/team send <name> <message>`

`team_send` 会向目标 agent 的邮箱写入一条 `msg_type="request"` 消息，并返回消息 id。

实现上的两个细节是：

- 发送到 `main` 时不会要求目标先注册
- 发送到其他名字时，会先通过 `TeammateManager.get()` 校验该队友已注册

返回文本类似：

```text
Sent message <message_id> to code-reviewer
```

### 主 REPL 如何接收队友响应

主循环每轮会调用 `_drain_team_mailbox()` 读取 `main` 邮箱中的新消息，并把它们打印成状态行，例如：

```text
Team response from code-reviewer: ...
Team request from planner [plan_approval]: ...
```

因此主 agent 和 teammate 之间的通信并不是通过特殊内部 API 完成的，而是同样经过邮箱文件。

## 10.6 当前边界与定位

BareAgent 当前的多智能体系统已经足够支持：

- 启动长期运行的队友
- 通过邮箱发起请求和接收响应
- 广播关闭
- 让队友顺手认领 `TaskManager` 中的 ready task

但它还不是一个复杂的编排平台。当前没有：

- 更细粒度的资源调度
- 跨进程运行状态发现
- 持久化的复杂协议状态机
- 自动的多阶段审批流

所以在阅读或扩展这部分代码时，最稳妥的心智模型仍然是：

- “邮箱 + 后台线程 + 轻量协议 + 可选任务认领”

与任务系统的结合见 [任务与 TODO](./ch12-tasks-todo.md)，与后台线程执行模型的细节见 [后台执行](./ch14-background.md)。

## 小结

BareAgent 的 team 系统由四层组件拼起来：

1. `TeammateManager` 负责定义谁是队友
2. `MessageBus` 负责把消息写进谁的邮箱
3. `ProtocolFSM` 负责把普通消息包装成 request / response / broadcast 协议
4. `AutonomousAgent` 负责让某个队友持续运行并处理消息或任务

下一章会回到单智能体也会受益的一项基础能力：当消息越来越长时，BareAgent 如何对上下文做微压缩和完整压缩，以避免 token 持续膨胀。
