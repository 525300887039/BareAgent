# 子智能体系统

## 智能体类型系统

<!-- AgentType 冻结数据类：tool_whitelist, tool_blacklist, max_turns, allow_nesting, permission_mode, system_prompt -->

## 内置类型

<!--
| 类型 | 工具范围 | 轮次 | 可嵌套 | 用途 |
|------|----------|------|--------|------|
| general-purpose | 全量 | 200 | 是 | 通用任务 |
| explore | 只读 | 50 | 否 | 代码探索 |
| plan | 只读 | 50 | 否 | 方案设计 |
| code-review | 只读 | 50 | 否 | 代码审查 |
-->

## 工具过滤

<!-- filter_tools() / filter_handlers()：白名单优先，黑名单排除，嵌套控制移除 subagent -->

## 权限隔离

<!-- PermissionGuard.for_subagent()：模式级联（PLAN→PLAN, AUTO→DEFAULT），fail-closed -->

## 后台异步执行

<!-- run_in_background=true 时通过 BackgroundManager 提交，主循环通过通知获取结果 -->

## 递归深度控制

<!-- max_depth=3，每层递减，达到 0 时禁止继续嵌套 -->

## 上下文压缩

<!-- 50k token 阈值触发，复用 Compactor 的微压缩 + LLM 摘要 -->

## 系统提示组合

<!-- 父级系统提示 + AgentType.system_prompt 拼接 -->
