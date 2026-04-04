# 配置系统

## 配置加载优先级

<!-- config.toml → config.local.toml → 环境变量 -->

## 配置段详解

### [provider]

<!-- 提供商、模型、API 密钥、base_url、wire_api -->

### [permission]

<!-- 权限模式、allow/deny 规则 -->

### [ui]

<!-- 流式输出、主题 -->

### [subagent]

<!-- 最大递归深度、默认类型 -->

### [thinking]

<!-- 思考模式、token 预算 -->

## 环境变量一览表

<!--
| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| BAREAGENT_CONFIG | 配置文件路径 | config.toml |
| BAREAGENT_PROVIDER | LLM 提供商 | anthropic |
| BAREAGENT_MODEL | 模型名称 | |
| BAREAGENT_API_KEY_ENV | API 密钥环境变量名 | |
| BAREAGENT_PERMISSION_MODE | 权限模式 | default |
| BAREAGENT_UI_STREAM | 流式输出 | true |
| BAREAGENT_UI_THEME | UI 主题 | |
| BAREAGENT_THINKING_MODE | 思考模式 | adaptive |
| BAREAGENT_THINKING_BUDGET_TOKENS | 思考 token 预算 | 10000 |
| BAREAGENT_SKILLS_DIR | 技能目录路径 | skills |
| BAREAGENT_SUBAGENT_MAX_DEPTH | 子智能体最大递归深度 | 3 |
| BAREAGENT_SUBAGENT_DEFAULT_TYPE | 子智能体默认类型 | general-purpose |
-->
