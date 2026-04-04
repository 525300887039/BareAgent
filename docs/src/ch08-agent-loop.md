# 核心智能体循环

## agent_loop() 执行流程

<!--
1. 组装系统提示（context.py）
2. 调用 LLM（流式/非流式）
3. 解析响应中的工具调用
4. 权限检查（PermissionGuard）
5. 执行工具处理器
6. 收集结果，追加到消息历史
7. 检查是否需要压缩
8. 重复直到无工具调用或达到 max_iterations
-->

## 迭代控制

<!-- max_iterations 默认 200，子智能体按 AgentType.max_turns 配置 -->

## 工具调用解析与分发

<!-- 从 LLMResponse.tool_calls 提取，匹配注册的工具处理器 -->

## 流式输出集成

<!-- StreamPrinter 实时渲染 LLM 输出 -->

## 后台任务注入

<!-- _run_background：每轮迭代开始时检查后台任务完成通知 -->
