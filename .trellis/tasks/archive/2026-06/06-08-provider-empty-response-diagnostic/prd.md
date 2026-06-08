# provider 空响应诊断（completed 但 text+tool 皆空时 warn）

## Goal

当 LLM 正常停止（end_turn/stop/completed）却既无文本又无工具调用时，BareAgent 当前**静默返回 `""`**、零提示。本任务加一个**非致命诊断**：在 agent loop 的单一收口点检测这种"退化空响应"并发一条 warning，帮使用者立刻意识到"模型没产出"（典型成因：wire_api/模型不匹配、relay 异常、模型拒答），而不是对着空白干瞪眼。直接来自 06-08 调试 ai8.my/krill 端点时踩的坑——`gpt-5.4-mini` 走 `responses` wire 时 `status=completed` 但 output 数组为空、`text=''`，花了好几轮才定位。

## What I already know

- 静默空返回点：`src/core/loop.py:114-117` —— `if not response.has_tool_calls: ... return response.text or ""`。`response.text` 为空 + 无工具调用 → 直接返回 `""`，无任何提示。
- 这里是**单一收口点**：chat_completions / responses 两种 wire × 流式/非流式都经 `_invoke_provider` 汇聚到此（流式已把增量累进 `response.text`，故空就是真空）。放这里一处覆盖全部，优于在多路径的 provider 层各加。
- 该 loop 被主循环 / 子代理 / 队友共用 → 诊断对三者统一生效。
- `loop.py` 当前**不用 logging**（无 import）；用 `console.print_*` 做用户可见输出。`UIProtocol` 方法：`print_assistant` / `print_tool_call` / `print_tool_result` / `print_error` / `print_status`。子代理/队友路径 `console=None`。
- `LLMResponse`（`base.py`）：`text` / `thinking` / `stop_reason` / `output_tokens` / `has_tool_calls`（property）/ `content_blocks`。

## Proposed Design（待 brainstorm 确认）

- **检测条件**：`not response.text and not response.has_tool_calls`（无文本 + 无工具调用）。命中即"退化空响应"。
- **位置**：`loop.py` 的 `if not response.has_tool_calls:` 分支内、`return` 之前。
- **呈现（双通道）**：`logging.warning(...)` 始终发（覆盖 console=None 的子代理/队友，进日志）；`console` 非 None 时额外 `console.print_status(...)`（用户在 REPL 看到）。严重度 = warning（**非致命**：仍照常 `return ""`，不抛、不重试，行为兼容）。
- **消息内容**：含 `stop_reason` + `output_tokens` + 通用 hint。`output_tokens > 0 但文本为空` 是今天的特征签名 → hint 提示"可能 wire_api/模型不匹配或 relay 异常"。
- **scope/配置**：默认始终开（无害诊断，无新配置项）。

## Decision (ADR-lite，全部已定)

- **Q1 条件边界 → (定) 不特判 thinking**：条件 = `not response.text and not response.has_tool_calls`。纯 thinking 无产出也算退化、照样 warn。理由：对使用者一样是"什么都没拿到"；条件最简最好测。
- **Q2 呈现+严重度 → (定) 双通道 + 非致命 + print_status**：`logging.warning(...)` 始终发（覆盖 console=None 的子代理/队友）；console 非 None 时额外 `console.print_status(...)`。仍照常 `return ""`，不抛不重试（行为兼容）。用 `print_status`（温和提示）而非 `print_error`（避免与真错误混淆）。
- **Q3 配置 → (定) 不加配置，始终开**：无害诊断、零正常路径开销（只在退化分支触发），不堆旋钮。
- **消息文案 → (定)**：`LLM returned an empty response (no text or tool calls) — stop_reason=<x>, output_tokens=<n>. Possible wire_api/model mismatch or relay issue.`

## Requirements (evolving)

- agent loop 在"正常停止但 text+tool 皆空"时发非致命诊断 warning（含 stop_reason/output_tokens/hint）。
- 不改变返回值与控制流（仍 `return ""`，不抛不重试）。
- 子代理/队友（console=None）经 logging 也能留下诊断。
- 正常（有文本或有工具调用）响应零行为变化、零额外输出。

## Acceptance Criteria (evolving)

- [ ] 构造一个 text 空 + 无 tool_calls 的 LLMResponse，跑 agent_loop → 触发一条 warning（断言 logging 或注入 console 收到），且返回值仍是 `""`。
- [ ] 正常响应（有 text）→ 无诊断、`print_assistant` 照常、返回文本不变。
- [ ] 有 tool_calls 的响应 → 不进空分支、无诊断。
- [ ] console=None（子代理路径）→ 仍发 logging.warning、不崩。
- [ ] 诊断消息含 stop_reason 与 output_tokens。
- [ ] ruff 干净，全量测试绿。

## Definition of Done

- 新增行为有 pytest 覆盖
- Lint / 全量测试 green
- 若加配置项 → config.toml + 文档；CLAUDE.md 视改动面决定是否补注

## Out of Scope (explicit)

- 自动重试空响应 / 自动切换 wire_api（仅诊断，不自愈）。
- provider 层逐路径检测（统一在 loop 收口点）。
- 把空响应升级为致命错误中断 loop（保持非致命，行为兼容）。
- 针对具体 relay 的特判（通用诊断）。

## Technical Notes

- 关键文件：`src/core/loop.py`（检测 + 双通道诊断；可能新增 `import logging` + module logger）、（视 Q3）`src/main.py` + `config.toml`（可选开关）。
- 参考既有 console 用法：`loop.py` 内 `console.print_error(msg)`（exceeded iterations）、`console.print_assistant`。
- 严重度与 fail-open 心智对齐 hooks/worktree：诊断是便利层，绝不改变安全/控制流。
