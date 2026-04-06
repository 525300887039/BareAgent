---
layout: home

hero:
  name: BareAgent
  text: 纯 Python 终端代码智能体
  tagline: 可插拔 LLM 提供商  细粒度权限控制  多智能体协调  可扩展技能系统
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/ch02-quickstart
    - theme: alt
      text: 项目概述
      link: /guide/ch01-overview
    - theme: alt
      text: GitHub
      link: https://github.com/525300887039/BareAgent

features:
  - title: 可插拔 LLM 提供商
    details: 支持 Anthropic、OpenAI、DeepSeek，统一接口，流式与非流式输出，配置即切换
  - title: 细粒度权限控制
    details: 四种权限模式（DEFAULT / AUTO / PLAN / BYPASS），内置危险命令检测，allow/deny 规则
  - title: 子智能体委派
    details: 4 种内置智能体类型，递归深度控制，权限隔离，后台异步执行
  - title: 多智能体协调
    details: 基于 JSONL 的消息总线，协议状态机，守护进程式自治智能体
  - title: 可扩展技能系统
    details: 从 skills/*/SKILL.md 自动发现，通过 load_skill 按需加载
  - title: 消息压缩与会话管理
    details: 微压缩 + LLM 摘要，50k token 阈值自动触发，会话持久化与恢复
---
