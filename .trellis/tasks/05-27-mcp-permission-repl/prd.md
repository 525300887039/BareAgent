# PR4: MCP 权限 + 子代理隔离 + REPL 命令

> 父任务：`.trellis/tasks/05-27-mcp`（完整 MCP 客户端规划，决策与研究见父 PRD + `research/`）
> 前置 PR：PR1（transport+protocol）✅、PR2（client+manager+registry+tools 注入）✅、PR3（resources+prompts）✅

## Goal

把 MCP 工具接入 BareAgent 的**安全治理三件套**：

1. **PermissionGuard 集成**：MCP 工具走现有四模式（DEFAULT/AUTO/PLAN/BYPASS），不应用 shell 风格 DANGEROUS_PATTERNS（args 是 JSON 不是 shell text）
2. **子代理隔离**：`AgentType` 增加 `mcp_tools_enabled: bool = True` 字段；`explore` / `plan` / `code-review` 三个只读类型设 `False`；`filter_tools` 按字段过滤 `mcp__*` 前缀
3. **REPL 管理命令**：`/mcp status` / `/mcp list` / `/mcp reload <name>` 三个空格分隔命令（与 PR3 的 `/mcp:` 冒号前缀互不冲突）

本 PR **不动**：MCP transport / protocol / client / registry 核心逻辑（除测试需要的辅助 hook），不动 loop / provider / multimodal（PR5），不做 atexit / payload 截断 / 进程崩溃恢复（PR6）。

## Requirements

### PermissionGuard 集成
- **DEFAULT 模式**：MCP 工具调用每次触发 `ask_user_fn`，参数预览走 `json.dumps(args, indent=2, ensure_ascii=False)`；单字段 value 超过 256 字符截断为 `<前 256>... [truncated, N chars]`（避免一个大 string 刷爆屏幕）
- **AUTO 模式**：MCP 工具自动通过（无 ask）
- **PLAN 模式**：MCP 工具一律拒绝（PLAN 只允许 `SAFE_TOOLS` 集合，MCP 工具有未知副作用不属于 SAFE_TOOLS）
- **BYPASS 模式**：放行（现有行为）
- **DANGEROUS_PATTERNS 不应用于 MCP 工具**：所有现有 pattern 都是 shell text 匹配，对 JSON args 无意义 + 误伤率高 —— `PermissionGuard.is_dangerous` 内部按 tool_name 是否 `mcp__` 前缀短路跳过
- **现有 allow/deny rules 透传**：`mcp__<server>__<tool>` 本身就是合法 tool name 前缀，用户可在 config.toml `[permission] deny = ["mcp__github__"]` 拒绝整个 github server 的工具——纯复用现有前缀匹配机制，无需新代码
- **fail_closed 子代理**：父 PRD 已规定后台子代理 `fail_closed=True`；PR4 维持此行为 + 子代理类型若 `mcp_tools_enabled=False`，则 MCP 工具在 `filter_tools` 阶段就被剔除（双层防御）

### 子代理隔离
- `AgentType` 数据类增加 `mcp_tools_enabled: bool = True` 字段
- `BUILTIN_AGENT_TYPES` 中：
  - `general-purpose` → `True`（默认全量）
  - `explore` / `plan` / `code-review` → `False`（read-only，禁用 MCP）
- `filter_tools(all_tools, agent_type)` 增加新过滤规则：当 `agent_type.mcp_tools_enabled is False` 时，剔除所有 `name.startswith("mcp__")` 的工具
- `filter_handlers` 透传（已按 `filtered_tools` 名单收敛）
- 用户自定义 agent type 若不显式设字段，默认 `True`（向后兼容）

### REPL 命令（`/mcp <subcommand>` 空格前缀，与 PR3 `/mcp:` 冒号前缀显式区分）
- 命令路由分支：`text == "/mcp" or text.startswith("/mcp ")` 触发（注意排除 `/mcp:` 冒号形式，那是 PR3 prompt 路由）
- 解析：`text.split()` 后 `parts[1:]` 是子命令 + args
- 子命令：
  - `/mcp status`：输出每个 server 一行 `<name>: <status> [<tool_count> tools, <resource_yes_no> resources, <prompt_count> prompts]`；server 状态来自 `MCPManager._status[name]`
  - `/mcp list`：按 server 分组列出所有可用 mcp 工具完整名（`mcp__<server>__<tool>`）；包含 PR3 自动注入的 `resource_list` / `resource_read`；prompts 列表也展示（`/mcp:<server>:<prompt>` 形式）
  - `/mcp reload <name>`：
    1. `manager.get_client(name)` 不存在 → 错误字符串 + 不重启
    2. 调 `manager.reload(name)`：关闭旧 client transport → 用同 server_config 重新构造 transport + client → 重新握手 + 拉 prompts/list（如 capability 在）
    3. 不重读 config.toml（v1 不做配置热重载，父 PRD 已锁）
    4. reload 失败 → server 标 `UNHEALTHY`，错误字符串反馈 + 不阻塞 REPL
  - `/mcp`（无子命令）或未知子命令 → 打印 usage `Usage: /mcp <status|list|reload>`
- 反馈走 `UIProtocol`（与现有 `/sessions` `/log` 命令同款），不用 `print()`

### MCPManager 增量
- 新增 `reload(name: str) -> None`：实现 PR4 reload 语义（close + rebuild + handshake + new prompts/list）
- 新增 `summarize() -> list[dict]`：返回每个 server 的概要 dict（name / status / tool_count / has_resources / prompt_count），给 `/mcp status` 使用
- 不动 PR2/PR3 现有 API 签名

## Acceptance Criteria

- [ ] DEFAULT 模式调用 `mcp__fetch__fetch` 工具触发 ask_user 提示，参数预览为格式化 JSON
- [ ] AUTO 模式调用 MCP 工具自动通过、不触发 ask
- [ ] PLAN 模式调用 MCP 工具被拒绝
- [ ] BYPASS 模式 MCP 工具放行
- [ ] 危险 args 测试：`mcp__shell__exec({"cmd": "rm -rf /"})` 在 DEFAULT 模式**只走 ask_user**，不被 DANGEROUS_PATTERNS 短路拒绝（PRD 决策：MCP args 不应用 shell pattern）
- [ ] `explore` / `plan` / `code-review` 三类子代理 `filter_tools` 后看不到任何 `mcp__*` 工具（双层防御：filter + permission_mode=PLAN）
- [ ] `general-purpose` 子代理仍能用 MCP 工具
- [ ] `/mcp status` 输出 server 状态 + 工具/resource/prompt 计数
- [ ] `/mcp list` 列出所有 `mcp__*` 工具 + 所有 `/mcp:<server>:<prompt>`
- [ ] `/mcp reload <name>`：模拟 transport 异常导致 server unhealthy → reload 后恢复 RUNNING + 工具重新出现在 manager
- [ ] `/mcp reload <unknown>` → REPL 错误字符串，不抛
- [ ] config.toml `[permission] deny = ["mcp__github__"]` 能拒绝 github server 所有工具
- [ ] 至少 10 个新 pytest case

## Definition of Done

- 改动集中于 `src/permission/guard.py` + `src/planning/agent_types.py` + `src/mcp/manager.py` + `src/main.py` REPL 段
- 新增测试 `tests/test_mcp_permission.py` + 扩展 `tests/test_agent_types.py`（如已存在）/ `tests/test_mcp_manager.py`
- `ruff check src tests` / `ruff format src tests` 全绿
- `pytest` 全集合 green，不退化 PR1-3 测试
- **禁动文件**：`src/core/loop.py` / `src/provider/*` / `src/mcp/transport/*` / `src/mcp/protocol.py` / `src/mcp/_sse.py` / `src/mcp/config.py` / `src/mcp/errors.py` / `src/mcp/registry.py` / `src/mcp/client.py`

## Technical Approach

### `src/permission/guard.py` 改动
- 增加常量 / 辅助：
  ```python
  def _is_mcp_tool(tool_name: str) -> bool:
      return tool_name.startswith("mcp__")
  ```
- `requires_confirm(tool_name, tool_input)`：
  - 现有逻辑保留
  - 在调 `is_dangerous` 之前判断：如果 `_is_mcp_tool(tool_name)` 且 mode 是 DEFAULT → 直接返回 `True`（必 ask），跳过 DANGEROUS_PATTERNS
- `is_dangerous(tool_name, tool_input)`：开头 short-circuit：`if _is_mcp_tool(tool_name): return False`
- `is_allowed(tool_name, mode)`（如果有这种内部方法）：PLAN 模式下，`_is_mcp_tool(tool_name)` 视为非 SAFE_TOOLS → 拒绝（实际上现有 PLAN 模式已经只放行 SAFE_TOOLS，无需改）
- ask_user 调用前的参数预览格式：在 ask_user_fn 接口或调用处加 `_format_mcp_preview(tool_input)` 辅助，json.dumps + 字段截断 256

### `src/planning/agent_types.py` 改动
- `AgentType` 加字段 `mcp_tools_enabled: bool = True`
- `_READ_ONLY_DEFAULTS` 加 `"mcp_tools_enabled": False`
- `filter_tools` 增加判断：`if not agent_type.mcp_tools_enabled and name.startswith("mcp__"): return False`

### `src/mcp/manager.py` 改动
- 新增 `reload(name: str)` 方法：
  ```python
  def reload(self, name: str) -> None:
      client = self._clients.get(name)
      server_cfg = next((s for s in self._cfg.servers if s.name == name), None)
      if server_cfg is None:
          raise MCPError(f"server '{name}' not in config")
      if client:
          client.close()  # idempotent per PR2
      self._status[name] = ServerStatus.STARTING
      try:
          new_client = self._build_client(server_cfg)
          new_client.start(timeout=server_cfg.start_timeout)
          self._clients[name] = new_client
          self._status[name] = ServerStatus.RUNNING
      except Exception as exc:
          self._status[name] = ServerStatus.UNHEALTHY
          # 通过 ui 反馈 + 不抛
          if self._ui:
              self._ui.print_error(f"reload '{name}' failed: {exc}")
          raise  # 让 REPL handler 决定是否捕获——manager 自己只标状态
  ```
  - `_build_client` 抽出 PR2 内的 client/transport 构造逻辑作为单独私有方法（如果当前是 inline 在 start_all 里就抽出）
- 新增 `summarize() -> list[dict[str, Any]]`：
  ```python
  return [
      {
          "name": name,
          "status": self._status[name].value,
          "tool_count": len(client._tools or []) if self._status[name] == ServerStatus.RUNNING else 0,
          "has_resources": client.has_capability("resources") if status == RUNNING else False,
          "prompt_count": len(client._prompts or []) if status == RUNNING else 0,
      }
      for name, client in self._clients.items()
  ]
  ```
  注意：访问 `_tools` / `_prompts` 私有字段——同模块/同包内可接受，避免对外暴露新公共 API（registry 里也已用此 pattern）

### `src/main.py` REPL 改动
- 在现有 slash 命令分支段（~line 1291-1452，与 PR3 `/mcp:` 同区域）追加 `/mcp` 空格前缀分支：
  ```python
  if text == "/mcp" or (text.startswith("/mcp ") and not text.startswith("/mcp:")):
      _dispatch_mcp_command(text, mcp_manager, ui)
      continue
  ```
- `_dispatch_mcp_command` 辅助函数定义在同文件（与 PR3 `_dispatch_mcp_prompt` 比邻）
  - 解析子命令
  - 调 `manager.summarize()` / 遍历 clients 拼输出 / 调 `manager.reload(name)` 包 try/except 反馈

### ask_user_fn 参数预览
- 现有 ask_user_fn 签名可能只接受 tool_name + raw input dict；查看实际签名（`src/main.py` 里组装 PermissionGuard 时传的 callable）决定在哪做格式化
- 最稳妥：在 PermissionGuard 内部 `_format_preview(tool_name, tool_input)` 辅助函数生成展示字符串，传给 ask_user_fn 作为额外参数；如果 ask_user_fn 当前签名固定，则把展示字符串拼到 ask 提示文本里（具体由实现 sub-agent 看代码定）

## Decision (ADR-lite)

**Context**：MCP 工具调用要接入 BareAgent 现有安全治理（PermissionGuard + agent_types），但 MCP 的语义与本地 shell/python 工具有几个根本差异：args 是 JSON 不是 shell 文本（DANGEROUS_PATTERNS 不适用）、副作用全权由远端 server 负责（client 无法做静态分析）、动态扩展（reload 需要支持）。

**Decision**：
- 三模式行为对齐父 PRD：DEFAULT ask / AUTO 通过 / PLAN 拒绝 / BYPASS 放行
- 完全跳过 DANGEROUS_PATTERNS：所有 pattern 都是 shell 语义、对 JSON 误伤率高，不适用
- 子代理隔离用单 bool 字段而非按工具白名单：MCP 工具是动态的（server 决定有哪些），白名单不可维护
- Reload 不读 config.toml：保持 v1 简单，配置热重载留 ROADMAP

**Consequences**：
- ✅ MCP 工具与现有 allow/deny rules 透明兼容（`mcp__github__` 前缀匹配）
- ✅ 子代理类型增加 `mcp_tools_enabled` 字段成本极低，向后兼容
- ✅ `/mcp reload` 给运维提供自助恢复机制，不需要重启整个 REPL
- ⚠️ DEFAULT 模式每次调用必 ask 可能用户骚扰——但 MCP 工具影响范围未知（远端 server 可任意操作），ask 是合理的最低防线；用户可配置 allow rule 提升信任的 server

## Out of Scope (explicit)

- **Per-server / per-tool 权限策略**：`[[mcp.servers]] auto_approve = [...]` 留 v2（父 PRD 已锁）
- **配置热重载**：编辑 config.toml 自动重载 server 留 ROADMAP 4.3
- **MCP server 沙箱**：v1 不限制 server 子进程的文件系统/网络访问，server 自负其责
- **Multimodal args 预览**：image / blob 在 args 中的预览（PR5 涉及多内容块时再考虑）
- **`/mcp reload --all`**：v1 只 reload 单个 server
- **Confirmation 持久化**："本次会话不再询问该工具" 之类的会话级 allow 列表 —— 留 v2
- **atexit / signal cleanup / payload 截断 / 进程崩溃自动恢复**：PR6
- **Multimodal 结果回传 / provider image 适配**：PR5

## Technical Notes

- 父任务 PRD 见 `../05-27-mcp/prd.md`
- 现有可借鉴 pattern：
  - `src/permission/guard.py::requires_confirm` 现有 DEFAULT / AUTO / PLAN / BYPASS 分支结构
  - `src/permission/rules.py` 现有前缀匹配
  - `src/planning/agent_types.py::filter_tools` 现有 disallowed_tools / nesting 过滤
  - `src/main.py` 现有 `/sessions` `/log` `/theme` 等 slash 命令分支模式 → 套给 `/mcp` 空格前缀
  - PR3 已落地 `_dispatch_mcp_prompt`（src/main.py）→ 新 `_dispatch_mcp_command` 比邻定义
- 必须遵循 `.trellis/spec/backend/`：
  - `error-handling.md`：reload 失败标 UNHEALTHY + 反馈不抛；自定义 MCPError 走现有层级
  - `logging-guidelines.md`：所有 REPL 反馈走 UIProtocol，capability 缺失 / unknown server 走 logger
  - `directory-structure.md`：所有改动落在 permission / planning / mcp/manager / main.py，无新模块
  - `quality-guidelines.md`：from __future__ + 类型注解 + ruff/pytest
