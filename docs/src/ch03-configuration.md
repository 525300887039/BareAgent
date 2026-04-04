# 配置系统

BareAgent 的配置由 `src/main.py` 统一解析。它既支持项目内的 TOML 文件，也支持环境变量和少量 CLI 参数覆盖。理解这一层的优先级，可以避免“为什么我改了配置却没有生效”这类问题。

## 3.1 配置加载优先级

配置加载可以分成两个阶段：先决定“读取哪一个配置文件”，再决定“最终值以哪一层为准”。

### 选择配置文件

BareAgent 按以下顺序决定主配置文件路径：

1. `--config <path>`
2. 环境变量 `BAREAGENT_CONFIG`
3. 项目自带的 `config.toml`

这一步只决定“基础配置文件”的位置。

### 合并本地覆盖文件

确定主配置文件后，BareAgent 会自动尝试读取同目录下的同名 `.local` 文件，并做递归合并。

例如：

- 主配置是 `config.toml` 时，会尝试合并 `config.local.toml`
- 主配置是 `configs/dev.toml` 时，会尝试合并 `configs/dev.local.toml`

合并规则是“同名键覆盖，嵌套 table 递归合并”。因此 `config.local.toml` 只需要写你想覆盖的字段，不必复制整份配置。

### 环境变量和 CLI 覆盖

在 TOML 合并完成后，BareAgent 还会继续应用覆盖层：

1. 环境变量覆盖单个字段
2. CLI 参数 `--provider`、`--model` 再次覆盖对应 provider 字段

因此，完整优先级可以理解为：

1. `--config` / `BAREAGENT_CONFIG` 选择配置文件
2. `config.toml`
3. `config.local.toml`
4. `BAREAGENT_*` 环境变量
5. `--provider`、`--model`

### 环境变量的类型转换

环境变量并不是简单的字符串拼接，加载时会按字段类型解析：

- 布尔值接受 `1`、`true`、`yes`、`on`，以及 `0`、`false`、`no`、`off`
- 整数字段使用 `int(...)` 解析
- 可选字符串字段在值为空字符串时会被视为 `None`

如果值格式不合法，`load_config()` 会直接报错，而不是静默忽略。

## 3.2 配置段详解

### 3.2.1 `[provider]`

`[provider]` 决定 BareAgent 通过哪个 LLM 接口工作。

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `string` | 提供商名称，当前支持 `openai`、`anthropic`、`deepseek` |
| `model` | `string` | 具体模型名，原样传给 provider |
| `api_key_env` | `string` | 存放 API Key 的环境变量名 |
| `base_url` | `string?` | OpenAI 兼容接口的基础地址，未设置时为 `None` |
| `wire_api` | `string?` | OpenAI 兼容接口的传输协议选择，例如 `responses` |

一个最小的官方 OpenAI 配置如下：

```toml
[provider]
name = "openai"
model = "gpt-4.1"
api_key_env = "OPENAI_API_KEY"
```

Anthropic 配置如下：

```toml
[provider]
name = "anthropic"
model = "claude-sonnet-4-20250514"
api_key_env = "ANTHROPIC_API_KEY"
```

如果你接的是 OpenAI 兼容网关，可以再加上 `base_url` 和 `wire_api`：

```toml
[provider]
name = "openai"
model = "gpt-5-codex-mini"
api_key_env = "OPENAI_API_KEY"
base_url = "https://right.codes/codex/v1"
wire_api = "responses"
```

实现细节上，三个 provider 的行为略有差异：

- `anthropic` 会构造 `AnthropicProvider`，并消费 `[thinking]` 配置
- `openai` 会构造 `OpenAIProvider`；若未设置 `wire_api`，默认走 `chat_completions`
- `deepseek` 也走 `OpenAIProvider`，但如果未显式设置 `base_url`，会自动使用 `https://api.deepseek.com`

关于默认值，需要区分两层含义：

- 当前仓库自带的 `config.toml` 示例值是 `openai` + `gpt-4.1`
- 如果你的配置文件根本没有 `[provider]`，源码兜底默认值是 `anthropic` + `claude-sonnet-4-20250514`

`api_key_env` 只存“环境变量名”，真正的 API Key 仍然必须出现在运行环境中。若环境变量不存在，`create_provider()` 会直接报错。

### 3.2.2 `[permission]`

`[permission]` 决定工具调用是否需要人工确认。

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | `string` | 权限模式，允许值为 `default`、`auto`、`plan`、`bypass` |
| `allow` | `string` 或 `array[string]` | 允许前缀规则 |
| `deny` | `string` 或 `array[string]` | 拒绝前缀规则 |

最常见的配置如下：

```toml
[permission]
mode = "default"
allow = ["bash(prefix:git status*)", "bash(prefix:pytest*)"]
deny = ["bash(prefix:npm publish*)"]
```

规则语法是：

```text
tool_name(prefix:command-prefix*)
```

例如：

- `bash(prefix:git status*)`
- `bash(prefix:pytest tests/*)`

当前实现的几个关键点：

- `tool_name` 在解析时会被规范化为小写，因此 `Bash(...)` 和 `bash(...)` 都能识别
- 末尾的 `*` 只是书写习惯，实际匹配逻辑是“命令是否以该前缀开头”
- `allow` 和 `deny` 当前只在 `bash` 的确认逻辑里生效，其他工具不会读取这些 prefix 规则
- 对 `bash` 而言，判断顺序是：`deny` -> 危险命令模式 -> `allow` -> 自动安全模式 -> 当前权限模式
- 因此危险命令模式会覆盖 `allow`，不会因为手工写了放行规则就绕过 fail-closed 检查

四种模式的详细行为见 [权限模型](./ch06-permission.md)。这里只需要记住：

- `default`：大多数写操作和未知命令需要确认
- `auto`：安全命令自动放行
- `plan`：只读
- `bypass`：不再询问确认

### 3.2.3 `[ui]`

`[ui]` 控制终端交互层的行为。

| 字段 | 类型 | 说明 |
|------|------|------|
| `stream` | `bool` | 是否优先使用 provider 的流式输出 |
| `theme` | `string` | UI 主题名 |

示例：

```toml
[ui]
stream = true
theme = "dark"
```

其中：

- `stream = true` 时，`agent_loop()` 会优先调用 `provider.create_stream()`；如果 provider 明确不支持流式，框架会自动回退到非流式调用
- `theme` 当前已经作为配置项和环境变量暴露，但在主 REPL 渲染路径里没有形成一个完整的主题切换系统，不应把它理解为“修改后会立刻切换 rich 主题”

### 3.2.4 `[subagent]`

`[subagent]` 控制子智能体的默认行为。

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_depth` | `int` | 子智能体递归深度上限 |
| `default_type` | `string` | 未显式指定 `agent_type` 时的默认类型 |

当前内置的子智能体类型有：

- `general-purpose`
- `explore`
- `plan`
- `code-review`

示例：

```toml
[subagent]
max_depth = 3
default_type = "general-purpose"
```

实现上，`default_type` 会在配置加载时校验；如果写成未知值，BareAgent 会直接拒绝启动，而不是等到运行时才发现。

### 3.2.5 `[thinking]`

`[thinking]` 定义扩展思考配置。

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | `string` | 允许值：`adaptive`、`enabled`、`disabled` |
| `budget_tokens` | `int` | 思考 token 预算 |

示例：

```toml
[thinking]
mode = "adaptive"
budget_tokens = 10000
```

这里需要注意 provider 差异：

- 在当前实现中，`AnthropicProvider` 会实际读取这两个字段，并把它们转换成 `thinking` 请求参数
- `OpenAIProvider` 和 `deepseek` 路径当前不会直接消费这组配置

换句话说，`[thinking]` 是全局配置项，但并不是所有 provider 都会以相同方式使用它。

## 3.3 环境变量一览表

下表汇总了当前代码中支持的主要环境变量。

| 变量名 | 说明 | 默认/备注 |
|--------|------|-----------|
| `BAREAGENT_CONFIG` | 指定主配置文件路径 | 未设置时使用项目自带 `config.toml` |
| `BAREAGENT_PROVIDER` | 覆盖 provider 名称 | 未设置时取配置文件值 |
| `BAREAGENT_MODEL` | 覆盖模型名 | 未设置时取配置文件值 |
| `BAREAGENT_API_KEY_ENV` | 覆盖 API Key 环境变量名 | 未设置时取配置文件值或 provider 默认值 |
| `BAREAGENT_BASE_URL` | 覆盖兼容接口基础地址 | 主要用于 OpenAI 兼容接口 |
| `BAREAGENT_WIRE_API` | 覆盖 OpenAI 兼容接口协议 | 例如 `responses` |
| `BAREAGENT_PERMISSION_MODE` | 覆盖权限模式 | 允许值：`default`、`auto`、`plan`、`bypass` |
| `BAREAGENT_UI_STREAM` | 覆盖是否流式输出 | 接受布尔字面量 |
| `BAREAGENT_UI_THEME` | 覆盖 UI 主题名 | 当前主要作为配置透传 |
| `BAREAGENT_THINKING_MODE` | 覆盖 thinking 模式 | 允许值：`adaptive`、`enabled`、`disabled` |
| `BAREAGENT_THINKING_BUDGET_TOKENS` | 覆盖 thinking token 预算 | 解析为整数 |
| `BAREAGENT_SKILLS_DIR` | 覆盖技能目录 | 未设置时按内置候选路径解析 |
| `BAREAGENT_SUBAGENT_MAX_DEPTH` | 覆盖子智能体最大深度 | 解析为整数 |
| `BAREAGENT_SUBAGENT_DEFAULT_TYPE` | 覆盖默认子智能体类型 | 必须是内置类型之一 |

另外，provider 自身的 API Key 通常放在下列环境变量中：

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `DEEPSEEK_API_KEY`

这些变量不以 `BAREAGENT_` 开头，因为它们并不是 BareAgent 的配置项，而是具体 provider 客户端在初始化时读取的密钥来源。

## 小结

如果你想最稳定地管理配置，推荐采用下面的方式：

1. 把公共默认值写进 `config.toml`
2. 把本机私有值写进 `config.local.toml`
3. 把真正的 API Key 放进环境变量
4. 只在临时切换 provider 或 model 时使用 CLI 参数

下一章将进入交互层，介绍 BareAgent REPL 的命令、快捷键和会话恢复机制。
