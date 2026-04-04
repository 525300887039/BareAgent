# BareAgent

纯 Python 终端代码智能体。支持可插拔 LLM 提供商、细粒度权限控制、多智能体协调和可扩展技能系统。

## 特性

- **多提供商支持** — Anthropic / OpenAI，统一接口，流式与非流式输出
- **内置工具** — bash、文件读写编辑、glob、grep，开箱即用
- **权限守卫** — 四种模式（default / auto / plan / bypass），危险命令自动拦截
- **多智能体协调** — 基于 JSONL 邮箱的消息总线，守护进程式自治智能体
- **子智能体委派** — 隔离上下文、递归深度限制、自动消息压缩
- **技能系统** — 从 `skills/*/SKILL.md` 自动发现，按需加载（code-review、git、test）
- **任务管理** — 持久化任务 + 会话级 TODO，支持依赖追踪和优先级
- **消息压缩** — 微压缩 + LLM 摘要，支撑超长对话

## 快速开始

### 环境要求

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)（推荐）

### 安装

```bash
uv pip install -e ".[dev]"
```

### 配置

设置 API 密钥环境变量：

```bash
# Linux / macOS
export OPENAI_API_KEY="your-key-here"
```

```powershell
# Windows PowerShell（当前会话）
$env:OPENAI_API_KEY="your-key-here"

# Windows PowerShell（永久生效）
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "your-key-here", "User")
```

默认配置在 `config.toml`，本地覆盖写入 `config.local.toml`（已 git-ignore）：

```toml
[provider]
name = "openai"
model = "gpt-4.1"
api_key_env = "OPENAI_API_KEY"

[permission]
mode = "default"

[ui]
stream = true
theme = "dark"

[thinking]
mode = "adaptive"
budget_tokens = 10000
```

也可通过环境变量覆盖任意配置项：

| 环境变量 | 说明 |
|---|---|
| `BAREAGENT_PROVIDER` | 提供商名称 |
| `BAREAGENT_MODEL` | 模型名称 |
| `BAREAGENT_API_KEY_ENV` | API 密钥环境变量名 |
| `BAREAGENT_PERMISSION_MODE` | 权限模式 |
| `BAREAGENT_UI_STREAM` | 是否流式输出 |
| `BAREAGENT_THINKING_MODE` | 思考模式（adaptive/enabled/disabled） |
| `BAREAGENT_THINKING_BUDGET_TOKENS` | 思考 token 预算 |
| `BAREAGENT_SKILLS_DIR` | 技能目录路径 |

### 运行

```bash
bareagent
# 或
python -m src.main
```

## 项目结构

```
src/
├── main.py                # 入口与 REPL 循环
├── core/                  # 智能体循环、工具注册、Schema、沙箱
│   ├── loop.py            #   核心 agent_loop()
│   ├── tools.py           #   工具注册与分发
│   ├── schema.py          #   工具 Schema 定义
│   ├── sandbox.py         #   路径安全检查
│   └── handlers/          #   各工具处理器实现
├── provider/              # LLM 提供商抽象
│   ├── base.py            #   BaseLLMProvider
│   ├── anthropic.py       #   Anthropic 实现
│   ├── openai.py          #   OpenAI 实现
│   └── factory.py         #   工厂
├── permission/            # 权限守卫
├── memory/                # 消息压缩与 token 计数
├── planning/              # 任务、TODO、技能、子智能体
├── team/                  # 多智能体协调
├── concurrency/           # 后台执行与通知
└── ui/                    # 终端 UI（rich + 流式）
skills/                    # 可扩展技能模块
tests/                     # pytest 测试
```

## 开发

```bash
# 测试
pytest
pytest tests/test_loop.py -k "test_name"

# 代码检查与格式化
ruff check src tests
ruff check --fix src tests
ruff format src tests
```

提交信息遵循 Conventional Commits：`Fix:`、`Feat:`、`Refactor:`、`Test:`、`Docs:`

## 许可证

MIT
