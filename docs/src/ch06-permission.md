# 权限模型

## 四种权限模式

<!--
| 模式 | 行为 |
|------|------|
| DEFAULT | 写操作需用户确认 |
| AUTO | 安全操作自动批准，危险操作仍需确认 |
| PLAN | 仅允许只读安全工具 |
| BYPASS | 无权限检查（危险） |
-->

## 安全工具白名单

<!-- SAFE_TOOLS：read_file, glob, grep, todo_read, task_list, task_get 等 -->

## 自动安全模式

<!-- AUTO_SAFE_PATTERNS：AUTO 模式下自动批准的 bash 命令模式 -->

## 危险命令检测

<!-- DANGEROUS_PATTERNS：rm -rf, force push, DROP TABLE 等 -->

## Allow/Deny 规则

<!-- 前缀匹配语法，配置文件中的 allow/deny 列表 -->

## 运行时模式切换

<!-- /default, /auto, /plan, /bypass 命令；Shift+Tab 快捷键 -->

## Fail-closed 机制

<!-- 后台智能体和子智能体场景下，无法交互确认时自动拒绝 -->
