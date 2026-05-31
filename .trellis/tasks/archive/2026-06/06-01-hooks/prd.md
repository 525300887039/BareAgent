# Hooks 系统（工具调用前后用户自定义钩子）

## Goal

让用户在 config.toml 声明 hooks：在工具调用前后触发自定义 shell 命令。PreToolUse 可**拦截**
工具执行（如挡危险操作），PostToolUse 可跑**副作用**（如 write_file 后自动 `ruff format`）。
这是让用户自定义智能体行为的核心扩展机制（ROADMAP 2.1，标"高优先级"）。

## What I already know（代码尽调结论）

- **工具执行插入点清楚**（`src/core/loop.py:104-143`，一个 user turn 的工具循环）：
  - 权限检查在 :109-115（`_requires_confirmation` + `_ask_permission`）。
  - 执行在 :130 `output = handler(**call.input)`，结果在 :141 `_tool_result(call.id, output)`。
  - **PreToolUse 插入点**：权限通过后（:116）、`handler` 前 → 可拦截（跳过 handler，把拒绝理由作为
    error result 回灌 LLM）。
  - **PostToolUse 插入点**：:131 `handler` 返回后、`_tool_result` 前 → 跑副作用。
  - **Stop 插入点**：:102 `agent_loop` 返回处。
- **config array-of-tables 模式现成**：`[[mcp.servers]]`/`[[lsp.servers]]` 经 `parse_mcp_config`/
  `parse_lsp_config` 解析（main.py:447-459），失败 graceful 降级。`[[hooks]]` 同构。
  Config dataclass 在 main.py（:138），加 `hooks: HooksConfig = field(default_factory=...)`。
- **子进程模式可复用**：`src/core/handlers/bash.py:run_bash`（Windows PowerShell + UTF-8 对齐 +
  timeout + capture）。hooks 执行借同样模式，但**走 JSON stdin** 传上下文。
- **权限是安全边界**：`PermissionGuard` 仍是主闸；hooks 是用户自配的便利层（trust-the-config，
  同 MCP server）。

## Requirements（evolving）

- 新建 `src/hooks/`：`events.py`（HookEvent 枚举）、`config.py`（HooksConfig + 解析 `[[hooks]]`）、
  `engine.py`（HookEngine：event+tool 匹配 → JSON stdin 跑 shell → 解释 exit code）。
- **PreToolUse**：权限通过后、handler 前触发；exit 2 = 拦截（skip handler，stderr 作拒绝理由回灌
  LLM 作 error result）；exit 0 = 放行；其他非 0 = 非阻塞警告但放行。
- **PostToolUse**：handler 返回后触发；跑副作用；非 0 仅警告不影响结果。
- JSON stdin 上下文：PreToolUse `{event,tool_name,tool_input,session_id,cwd}`；PostToolUse 追加
  `{tool_output,is_error}`。字段名对齐 Claude Code（tool_name/tool_input）便于迁移认知。
- `agent_loop` 加可选 `hook_engine` 参数；main.py 建 engine 传入主循环；**子代理不传**（隔离，
  hooks 只在主循环触发）。
- config.toml `[[hooks]]` 示例 + CLAUDE.md 文档 + 解析失败 graceful 降级。
- 单测覆盖：匹配（event/tool）、PreToolUse 拦截/放行/警告、PostToolUse 副作用、JSON stdin、
  超时/spawn 失败降级、config 解析。

## Acceptance Criteria（evolving）

- [ ] `[[hooks]]` 解析为 HooksConfig；非法配置 graceful 降级不崩。
- [ ] PreToolUse exit 2 拦截工具：handler 不执行，LLM 收到拒绝理由（error result）。
- [ ] PreToolUse exit 0 放行；其他非 0 → 警告 + 放行（非阻塞）。
- [ ] PostToolUse 在 handler 后触发并能跑副作用；其退出码不改变工具结果。
- [ ] hook 收到正确 JSON stdin（event/tool_name/tool_input[/tool_output/is_error]）。
- [ ] event + 可选 tool 名匹配正确（tool 省略 = 匹配所有工具）。
- [ ] hook 超时 / spawn 失败 → 非阻塞警告 + 放行（fail-open，见 D3），不挂主循环。
- [ ] 子代理 agent_loop 不触发 hooks。
- [ ] ruff / pytest / pyright 全绿；新行为有测试。

## Definition of Done

- 单测覆盖匹配/Pre 拦截/Post 副作用/JSON stdin/超时降级/config 解析；ruff·pytest·pyright 绿。
- config.toml `[[hooks]]` 示例段 + CLAUDE.md 段。
- 跨平台子进程（复用 bash.py 的 Windows PowerShell + UTF-8 对齐）。无新依赖。

## Decision (ADR-lite)

**Context**: Hooks 触及 agent_loop 工具执行核心，需定义事件范围、控制协议、失败模式三大边界；
权限闸是既有安全边界，hooks 为用户自配便利层。

**Decisions（已与用户确认，全部按推荐 A）**:
- D1 — **事件范围 = PreToolUse + PostToolUse**。覆盖两个高价值场景（挡危险 / 写后自动格式化），
  面最小；Stop/Notification/其余事件留后续扩展位。
- D2 — **控制协议 = exit-code 制**（对齐 Claude Code）：exit 0 放行 / **exit 2 拦截**（PreToolUse
  跳过 handler，stderr 作拒绝理由回灌 LLM）/ 其他非 0 非阻塞警告但放行。MVP **不支持**改写
  tool_input（JSON-stdout 高级协议留后续）。
- D3 — **失败模式 = fail-open**：hook spawn 失败/超时 → 警告 + 放行。理由：hooks 是便利层非安全
  边界（PermissionGuard 才是主闸），且对齐 Claude Code，避免误伤挂住主循环。

**已直接定（非问题项）**:
- 配置走 config.toml `[[hooks]]`（不引 settings.json）；仅精确 tool 名匹配（省略=匹配所有）；
  **子代理不触发 hooks**（隔离，同 token_tracker 只在主循环）；仅 JSON stdin（不注入 env 变量）。

**Consequences**: MVP 交付「挡危险 + 写后副作用」核心价值，面收敛；JSON-stdout 高级协议、输入改写、
其余事件、子代理 hooks、env 注入、热重载均为后续扩展位。

## Out of Scope（explicit）

- JSON-stdout 高级协议、PreToolUse 改写 tool_input（除非 D2 改判）。
- Stop / Notification / UserPromptSubmit / SessionStart 等其余事件（除非 D1 改判）。
- 子代理内触发 hooks；hook 并行/异步执行；env 变量注入（仅 JSON stdin）。
- settings.json（统一用 config.toml）；regex/glob tool 匹配（MVP 仅精确 tool 名 / 省略=全部）。
- 配置热重载（启动时加载）。

## Technical Notes

- 关键文件：`src/hooks/{__init__,events,config,engine}.py`(新)、`src/core/loop.py`(Pre/Post 插入 +
  hook_engine 参数)、`src/main.py`(HooksConfig + Config 字段 + 解析 + 建 engine 传主循环)、
  `config.toml`(示例)、`CLAUDE.md`、`tests/test_hooks_*.py`(新)。
- 复用：`src/core/handlers/bash.py` 子进程模式（Windows PowerShell + UTF-8 + timeout）。
- 排序：权限检查（现有）先于 PreToolUse hook，二者都通过才执行 handler（hook 加限制，不能越过
  权限拒绝）。
- 参考模型：Claude Code hooks（PreToolUse/PostToolUse、JSON stdin、exit 2 = block）—— 权威约定，
  字段名/语义对齐以便用户迁移。
