# 快速开始

本章目标是让你在几分钟内把 BareAgent 跑起来，并完成第一次交互。更细的配置项、权限模型和 REPL 命令，会在后续章节展开。

## 2.1 环境要求

运行 BareAgent 需要以下环境：

- Python 3.12 或更高版本
- [uv](https://github.com/astral-sh/uv) 包管理器
- 可用的 LLM API Key

BareAgent 当前在代码中内置支持以下提供商：

- `openai`
- `anthropic`
- `deepseek`

本文的快速开始分两条路径：

- **路径 A：官方 API**。适合大多数首次使用者，直接使用 OpenAI 或 Anthropic 的官方接口
- **路径 B：OpenAI 兼容代理**。适合已经有兼容 OpenAI 协议的网关或代理服务的场景

## 2.2 安装

在仓库根目录执行：

```bash
uv pip install -e ".[dev]"
```

这会以可编辑模式安装 BareAgent，并带上 `pytest`、`ruff` 等开发依赖。

安装完成后，可以先验证命令行入口是否可用：

```bash
bareagent --help
```

如果当前环境没有把脚本目录加入 `PATH`，也可以直接使用模块入口：

```bash
python -m src.main --help
```

当前 CLI 支持的基础参数如下：

- `--provider`：临时覆盖配置中的提供商名称
- `--model`：临时覆盖配置中的模型名称
- `--config`：指定自定义 TOML 配置文件路径

## 2.3 配置 API 密钥

BareAgent 的配置按以下顺序生效：

1. `config.toml`
2. `config.local.toml`
3. 环境变量

通常建议把公共默认值放在 `config.toml`，把本机私有配置写进 `config.local.toml`。当前仓库已经包含一份 `config.toml`，并且 `.gitignore` 已忽略 `config.local.toml`。

需要注意两个“默认值”：

- 仓库自带的 `config.toml` 当前预设为 `openai` + `gpt-4.1`
- 如果你换成自己的配置文件，且没有提供 provider 相关字段，源码中的兜底默认值是 `anthropic` + `claude-sonnet-4-20250514`

### 路径 A：使用官方 API

如果你准备直接接 OpenAI 或 Anthropic 官方接口，推荐在 `config.local.toml` 中只覆盖最小必要配置。

OpenAI 示例：

```toml
[provider]
name = "openai"
model = "gpt-4.1"
api_key_env = "OPENAI_API_KEY"
```

Anthropic 示例：

```toml
[provider]
name = "anthropic"
model = "claude-sonnet-4-20250514"
api_key_env = "ANTHROPIC_API_KEY"
```

然后设置环境变量。

Linux / macOS：

```bash
export OPENAI_API_KEY="your-key-here"
# 或
export ANTHROPIC_API_KEY="your-key-here"
```

Windows PowerShell：

```powershell
$env:OPENAI_API_KEY="your-key-here"
# 或
$env:ANTHROPIC_API_KEY="your-key-here"
```

如果你是通过 `--provider` 或 `BAREAGENT_PROVIDER` 临时切换提供商，BareAgent 会按提供商名称匹配默认的 API Key 环境变量：

- `openai` -> `OPENAI_API_KEY`
- `anthropic` -> `ANTHROPIC_API_KEY`
- `deepseek` -> `DEEPSEEK_API_KEY`

如果你是直接编辑配置文件切换 `provider.name`，请同时确认 `api_key_env` 也同步修改。

### 路径 B：使用 OpenAI 兼容代理

如果你使用 OpenAI 兼容代理，可以在 `config.local.toml` 中写成这样：

```toml
[provider]
name = "openai"
model = "gpt-5-codex-mini"
api_key_env = "OPENAI_API_KEY"
base_url = "https://right.codes/codex/v1"
wire_api = "responses"
```

这类配置适用于“接口形状兼容 OpenAI，但实际不是官方域名”的服务。这里有两个关键点：

- `base_url` 指向你的兼容网关地址
- `wire_api = "responses"` 表示通过 OpenAI Responses API 兼容层发送请求

即使是兼容代理，这条路径仍然默认读取 `OPENAI_API_KEY`。

如果你的兼容服务走的是 Chat Completions 风格，而不是 Responses 风格，可以不设置 `wire_api`，或在第 3 章按需调整。

## 2.4 首次运行

配置好 API Key 后，直接启动：

```bash
bareagent
```

或：

```bash
python -m src.main
```

启动成功后，你会看到类似输出：

```text
BareAgent REPL (openai/gpt-4.1)
Permission mode: default. Type /help to see available commands.
bareagent>
```

现在可以进行第一次对话：

```text
bareagent> 请用一句话介绍你自己
```

也可以让它先做一个简单的本地任务，例如查看当前目录：

```text
bareagent> 请查看当前目录下有哪些文件
```

首次使用时，建议先掌握这几个交互入口：

- `/help`：查看所有斜杠命令
- `/exit`：退出 REPL
- `/plan`：切换到只读的规划模式
- `Shift+Tab`：在交互终端中循环切换权限模式

默认权限模式来自配置文件；当前仓库自带配置的默认值是 `default`。如果你在首次试用时只想先观察行为，不想允许写操作，可以先输入 `/plan`。

完成第一轮对话后，BareAgent 会在当前工作目录下创建 `.transcripts/` 保存会话快照，后续可以用 `/sessions` 和 `/resume` 恢复历史会话。

## 下一步

跑通第一个会话后，建议继续阅读：

- [配置系统](./ch03-configuration.md)
- [REPL 交互](./ch04-repl.md)
- [权限模型](./ch06-permission.md)
