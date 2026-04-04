# 工具系统

## 基础工具

<!--
| 工具名 | 说明 |
|--------|------|
| bash | 执行 shell 命令 |
| read_file | 读取文件内容 |
| write_file | 写入文件 |
| edit_file | 编辑文件（精确替换） |
| glob | 文件模式匹配搜索 |
| grep | 文件内容搜索 |
-->

## 延迟加载工具

<!--
按需注册，首次使用时加载：
- todo_write / todo_read — 会话级 TODO
- task_create / task_list / task_get / task_update — 持久化任务
- subagent — 子智能体委派
- load_skill — 技能加载
- background_run — 后台执行
- team_spawn / team_send / team_list — 多智能体协调
-->

## 工具 Schema 定义

<!-- core/schema.py：每个工具的 JSON Schema 定义，包含参数类型、描述、必填项 -->

## 工具处理器

<!-- core/handlers/ 目录下各处理器的职责划分 -->
