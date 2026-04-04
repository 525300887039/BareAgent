# REPL 交互

BareAgent 的主入口是一个交互式 REPL。启动后，程序会显示当前 provider/model、权限模式提示，并进入 `bareagent>` 输入循环。

这一章关注的是“你在 REPL 中能做什么”，以及每个命令背后的真实运行行为。

## 4.1 斜杠命令一览

BareAgent 在读取到以 `/` 开头的输入后，会优先把它当作本地控制命令，而不是发送给 LLM。

### 命令总表

| 命令 | 作用 | 说明 |
|------|------|------|
| `/help` | 显示帮助信息 | 打印所有可用斜杠命令和快捷键 |
| `/exit` | 退出程序 | 广播团队关闭消息后退出 REPL |
| `/clear` | 清屏并开始新会话 | 当前实现与 `/new` 等价 |
| `/new` | 开始新会话 | 重置消息、TODO、session id 和邮箱 |
| `/compact` | 手动触发上下文压缩 | 压缩后立即保存快照 |
| `/default` | 切换到 DEFAULT 模式 | 直接修改当前权限模式 |
| `/auto` | 切换到 AUTO 模式 | 直接修改当前权限模式 |
| `/plan` | 切换到 PLAN 模式 | 只读模式 |
| `/bypass` | 切换到 BYPASS 模式 | 关闭确认提示 |
| `/mode` | 交互式选择权限模式 | 在下一次输入中输入 `1` 到 `4` 选择模式 |
| `/sessions` | 列出历史会话 | 只显示 session id 列表 |
| `/resume [session_id]` | 恢复历史会话 | 省略参数时恢复最近一次会话 |
| `/team ...` | 管理多智能体队友 | 支持 `list`、`spawn`、`send` |

### `/clear` 与 `/new`

这两个命令在当前实现中走同一条逻辑分支，效果相同：

- 将消息历史重置为初始系统消息
- 清空当前会话级 TODO
- 生成新的 session id
- 切换到新的团队邮箱目录
- 重新绑定 handlers
- 清空终端画面

因此，`/clear` 更偏向“清屏并重新开始”的心智模型，`/new` 更偏向“开始一个新会话”的心智模型，但代码行为是一致的。

### `/compact`

`/compact` 会强制触发一次上下文压缩，而不是等待 token 阈值自动命中。

执行完成后，REPL 会：

- 对当前消息历史执行压缩
- 立即保存新的 transcript 快照
- 重新构建 handlers，以保证运行时对象仍与当前消息状态一致

压缩策略本身见 [消息压缩](./ch11-compaction.md)。

### `/mode` 与直接模式切换

权限模式有两种切换方式：

- 直接输入 `/default`、`/auto`、`/plan`、`/bypass`
- 输入 `/mode`，进入交互式菜单

需要特别注意：`/mode` 不是“显示当前模式”的快捷命令，而是一个真正的交互式选择器。输入它以后，REPL 会打印 1 到 4 的编号菜单，并读取下一次输入作为选择结果。

### `/resume`

`/resume` 有两种用法：

```text
/resume
/resume <session_id>
```

行为分别是：

- 不带参数时：恢复最近一次保存的会话
- 带 `session_id` 时：恢复指定会话的最新快照

恢复成功后，当前消息历史会被整段替换，后续 transcript 快照会继续沿用被恢复的 session id。

### `/team`

`/team` 是多智能体管理入口，当前支持三个子命令：

```text
/team list
/team spawn <name>
/team send <name> <message>
```

它们分别对应：

- 列出已注册队友及其运行状态
- 启动一个已注册的自治队友
- 向某个队友邮箱发送消息

更完整的消息总线、协议状态机和自治循环见 [多智能体协调](./ch10-team.md)。

### 斜杠命令补全

当 REPL 运行在真正的 TTY 环境中时，BareAgent 会启用 `prompt_toolkit` 的补全器：

- 只有输入以 `/` 开头时，才会触发斜杠命令补全
- 当前补全集合包含 `/help`、`/exit`、`/clear`、`/new`、`/compact`、权限模式命令、`/mode`、`/sessions`、`/resume` 和 `/team`

如果运行环境不是 TTY，REPL 会回退到普通 `input()`，这时没有命令补全和快捷键绑定。

## 4.2 快捷键

### `Shift+Tab`

`Shift+Tab` 会在 REPL 中循环切换权限模式，顺序固定为：

```text
default -> auto -> plan -> bypass -> default
```

当前实现的一个细节是：切换模式时不会清空你已经输入到一半的 prompt buffer。也就是说，你可以一边写问题，一边临时调整权限模式，而不会丢失草稿。

### `Ctrl+Z`

在启用了 `prompt_toolkit` 的交互环境中，`Ctrl+Z` 被显式绑定为“退出当前 REPL”。实现方式是直接抛出 `EOFError`，随后主循环走统一的退出路径。

### `Ctrl+C`

`Ctrl+C` 不是立即退出：

- 第一次按下时，只会中断当前输入或当前 agent loop，并给出提示
- 连续第二次按下时，REPL 才会真正退出

这能减少误触导致的整段会话丢失，尤其是在 agent 正在长时间思考或执行工具时。

## 4.3 会话管理

会话管理由 `TranscriptManager` 驱动，核心目标是：每轮成功交互后都能恢复到最近状态。

### transcript 保存位置

BareAgent 会在当前工作目录下自动创建：

```text
.transcripts/
```

目录不存在时会自动创建，不需要手工准备。

### 文件命名与 session id

每个 transcript 快照都是一个 JSONL 文件，命名格式为：

```text
<session_id>_<timestamp>.jsonl
```

其中：

- `session_id` 由时间戳加随机后缀组成，例如 `20260404-120000-123456-abc123`
- `timestamp` 是保存快照时的本地时间戳

同一个 session 可以有多个快照文件，`TranscriptManager` 会总是选择时间最新的一份作为该 session 的当前状态。

### 何时保存快照

当前实现中，会在这些时机写入 transcript：

- 一轮对话成功完成后
- 手动执行 `/compact` 后

如果 LLM 调用失败，或者 agent loop 被中断，当前轮新增消息会被回滚，不会保存成新的快照。

### `/sessions`

`/sessions` 会扫描 `.transcripts/*.jsonl`，按“每个 session 最新快照的时间”倒序列出 session id。

它返回的是逻辑 session 列表，而不是全部快照文件名。因此你看到的是：

- `session-a`
- `session-b`

而不是：

- `session-a_2026-04-05T12-00-00.jsonl`
- `session-a_2026-04-05T12-10-00.jsonl`

### `/resume`

恢复逻辑是：

1. 找到目标 session
2. 取该 session 最新的快照文件
3. 用其中的消息历史覆盖当前内存状态
4. 让后续 transcript 继续写回同一个 session

这意味着 `/resume` 不是“导入一份历史副本”，而是“回到那条会话线上继续工作”。

### 会话重置与隔离

`/new` 或 `/clear` 除了重置消息历史，还会做两件重要的隔离动作：

- 生成新的 session id，避免后续 transcript 写回旧会话
- 切换到新的团队邮箱目录，避免上一会话的多智能体消息泄漏到新会话

因此，如果你在旧会话里已经启动过 teammate，重新开一个新会话后，新旧消息流是隔离的。

## 小结

BareAgent REPL 的本地控制面主要由三部分组成：

- 斜杠命令：负责会话、压缩、模式和团队管理
- 快捷键：负责快速切换权限和退出
- transcript：负责把会话持久化到 `.transcripts/`

下一章会继续向下走一层，介绍 LLM 实际可调用的工具 schema、handler 绑定方式，以及基础工具与延迟初始化工具的组织结构。
