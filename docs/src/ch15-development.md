# 开发指南

## 项目结构

<!--
src/
├── main.py                # 入口与 REPL 循环
├── core/                  # 智能体循环、工具注册、Schema、沙箱
│   ├── loop.py
│   ├── tools.py
│   ├── schema.py
│   ├── context.py
│   ├── sandbox.py
│   ├── fileutil.py
│   └── handlers/
├── provider/              # LLM 提供商抽象
│   ├── base.py
│   ├── anthropic.py
│   ├── openai.py
│   └── factory.py
├── permission/            # 权限守卫
│   ├── guard.py
│   └── rules.py
├── memory/                # 消息压缩与会话管理
│   ├── compact.py
│   ├── token_counter.py
│   └── transcript.py
├── planning/              # 任务、TODO、技能、子智能体
│   ├── agent_types.py
│   ├── subagent.py
│   ├── tasks.py
│   ├── todo.py
│   └── skills.py
├── team/                  # 多智能体协调
│   ├── mailbox.py
│   ├── autonomous.py
│   ├── manager.py
│   └── protocols.py
├── concurrency/           # 后台执行与通知
│   ├── background.py
│   └── notification.py
└── ui/                    # 终端 UI
    ├── console.py
    └── stream.py
-->

## 开发环境搭建

<!--
git clone ...
cd BareAgent
uv pip install -e ".[dev]"
-->

## 测试

<!--
pytest                             # 全部测试
pytest tests/test_loop.py          # 单个文件
pytest tests/test_loop.py -k "test_name"  # 单个测试
-->

## 代码检查与格式化

<!--
ruff check src tests               # 检查
ruff check --fix src tests          # 自动修复
ruff format src tests               # 格式化
-->

## 提交规范

<!--
Conventional Commits：
- Fix: 修复 bug
- Feat: 新功能
- Refactor: 重构
- Test: 测试
- Docs: 文档
-->

## 扩展点

### 新增 LLM 提供商

<!-- 继承 BaseLLMProvider，实现 create() 和 create_stream()，在 factory.py 注册 -->

### 新增工具

<!-- 在 core/schema.py 定义 Schema，在 core/handlers/ 实现处理器，在 core/tools.py 注册 -->

### 新增智能体类型

<!-- 在 planning/agent_types.py 的 BUILTIN_AGENT_TYPES 中添加 AgentType 实例 -->

### 新增技能

<!-- 在 skills/ 下创建目录，编写 SKILL.md -->
