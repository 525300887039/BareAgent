# BareAgent 项目文档大纲

## 1. 概述

- 1.1 项目简介
- 1.2 核心特性
- 1.3 架构总览（流程图：用户输入 → REPL → agent_loop → LLM → 工具执行 → 输出）

## 2. 快速开始

- 2.1 环境要求（Python 3.12+、uv）
- 2.2 安装
- 2.3 配置 API 密钥
- 2.4 首次运行

## 3. 配置系统

- 3.1 配置加载优先级（config.toml → config.local.toml → 环境变量）
- 3.2 配置段详解
  - 3.2.1 [provider] — 提供商、模型、API 密钥、base_url、wire_api
  - 3.2.2 [permission] — 权限模式、allow/deny 规则
  - 3.2.3 [ui] — 流式输出、主题
  - 3.2.4 [subagent] — 最大递归深度、默认类型
  - 3.2.5 [thinking] — 思考模式、token 预算
- 3.3 环境变量一览表

## 4. REPL 交互

- 4.1 斜杠命令一览（/help, /exit, /clear, /new, /compact, /sessions, /resume, /team, /mode, /default, /auto, /plan, /bypass）
- 4.2 快捷键（Shift+Tab 切换权限模式）
- 4.3 会话管理（会话 ID、转录持久化、恢复）

## 5. 工具系统

- 5.1 基础工具（bash, read_file, write_file, edit_file, glob, grep）
- 5.2 延迟加载工具（todo_*, task_*, subagent, load_skill, background_run, team_*）
- 5.3 工具 Schema 定义（core/schema.py）
- 5.4 工具处理器（core/handlers/）

## 6. 权限模型

- 6.1 四种权限模式（DEFAULT / AUTO / PLAN / BYPASS）
- 6.2 安全工具白名单（SAFE_TOOLS）
- 6.3 自动安全模式（AUTO_SAFE_PATTERNS）
- 6.4 危险命令检测（DANGEROUS_PATTERNS）
- 6.5 Allow/Deny 规则（前缀匹配语法）
- 6.6 运行时模式切换
- 6.7 Fail-closed 机制（后台/子智能体场景）

## 7. LLM 提供商

- 7.1 抽象基类 BaseLLMProvider
- 7.2 AnthropicProvider
- 7.3 OpenAIProvider
- 7.4 工厂模式（factory.py）
- 7.5 LLMResponse 统一响应结构
- 7.6 流式 vs 非流式（create_stream / create）
- 7.7 扩展思考（ThinkingConfig）

## 8. 核心智能体循环

- 8.1 agent_loop() 执行流程
- 8.2 迭代控制（max_iterations）
- 8.3 工具调用解析与分发
- 8.4 流式输出集成
- 8.5 后台任务注入（_run_background）

## 9. 子智能体系统

- 9.1 智能体类型系统（AgentType 数据类）
- 9.2 内置类型（general-purpose / explore / plan / code-review）
- 9.3 工具过滤（白名单、黑名单、嵌套控制）
- 9.4 权限隔离（for_subagent、模式级联）
- 9.5 后台异步执行（run_in_background + BackgroundManager）
- 9.6 递归深度控制（max_depth）
- 9.7 上下文压缩（50k token 阈值、Compactor 复用）
- 9.8 系统提示组合（父级 + 类型提示）

## 10. 多智能体协调

- 10.1 消息总线（MessageBus — JSONL 邮箱）
- 10.2 协议状态机（ProtocolFSM — PLAN_APPROVAL / SHUTDOWN）
- 10.3 自治智能体（AutonomousAgent — 空闲-轮询-认领循环）
- 10.4 TeammateManager（注册、生成、管理）
- 10.5 /team 命令（list / spawn / send）

## 11. 消息压缩

- 11.1 微压缩（截断旧工具结果）
- 11.2 完整压缩（LLM 摘要生成）
- 11.3 触发策略（token 阈值 50k）
- 11.4 保留策略（系统消息、近期上下文）
- 11.5 Token 估算（token_counter.py）

## 12. 任务与 TODO

- 12.1 TaskManager — 持久化 JSON 存储、状态流转、依赖追踪
- 12.2 TodoManager — 会话级内存存储、优先级、提醒
- 12.3 对应工具（task_create/list/get/update, todo_write/read）

## 13. 技能系统

- 13.1 技能发现（skills/*/SKILL.md 自动扫描）
- 13.2 技能加载（load_skill 工具按需加载）
- 13.3 内置技能（code-review / git / test）
- 13.4 自定义技能编写指南

## 14. 后台执行

- 14.1 BackgroundManager（threading 模型）
- 14.2 任务提交与通知（submit / drain_notifications）
- 14.3 NotificationManager

## 15. 开发指南

- 15.1 项目结构
- 15.2 开发环境搭建
- 15.3 测试（pytest，29 个测试文件）
- 15.4 代码检查与格式化（ruff）
- 15.5 提交规范（Conventional Commits）
- 15.6 扩展点
  - 新增 LLM 提供商
  - 新增工具
  - 新增智能体类型
  - 新增技能
