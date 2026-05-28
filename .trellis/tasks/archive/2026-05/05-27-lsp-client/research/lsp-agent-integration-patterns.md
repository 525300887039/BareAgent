# Research: AI Agent + LSP Integration Patterns

- **Query**: 主流 AI coding agent / Python LSP client 库怎么集成 LSP, 把 LSP 能力暴露给 LLM
- **Scope**: external (GitHub repos + READMEs + source spot-checks)
- **Date**: 2026-05-27

## TL;DR

1. **Python 客户端只有一个真正的候选**: `multilspy`(Microsoft, 578★, 2026-04 活跃) 提供"高层语义 API + 内置 server 下载/启动" 的体验; `pylspclient`(141★, 2025-06 后只剩 dependabot 提交) 仅适合做底层 transport; `sansio-lsp-client`(29★) 是教科书式的 sans-io 设计但只在 Porcupine 一个项目活跃. 真正生产级、给 AI agent 设计的是 Serena 内部 fork 的 **`solidlsp`** (源于 multilspy + OLSP, 主仓 24k★, 2026-05-27 仍在 daily 提交).
2. **暴露策略行业共识 = Hybrid**: 核心几把(`find_symbol` / `find_references` / `get_diagnostics` / `rename`)做 LLM tool, 写文件类工具自动跑一次 diagnostics diff 注入到 tool result 里. Cursor / Cline / Continue 走"嵌入 IDE 借 VSCode `executeCommand` 通道"的路, Serena 走"独立进程, 自己管 LSP, 通过 MCP 暴露"的路 -- BareAgent 没有 IDE 宿主, 只能走 Serena 路径.
3. **价值密度排序**: `documentSymbol` + `definition` + `references` + `diagnostics` 四件套 = MVP 必做; `hover` / `workspace/symbol` / `rename` = v2; `completion` / `signatureHelp` / `codeAction` / `formatting` 在 LLM 循环里 ROI 很低, 可不做.

---

## 1. Python LSP Client 库对比

| 库 | 仓库 | ★ | 最近 commit | Framing | Lifecycle 完整度 | Server-initiated request | 多 server | 推荐度 |
|---|---|---|---|---|---|---|---|---|
| **multilspy** | [microsoft/multilspy](https://github.com/microsoft/multilspy) | 578 | 2026-04-13 | Content-Length 完整 ([server.py](https://github.com/microsoft/multilspy/blob/main/src/multilspy/lsp_protocol_handler/server.py)) | initialize / initialized / didOpen / shutdown / exit, 内置 13 个语言 server 的下载与配置 | 注册 notification + request handler(`on_notification` / `on_request`) | 单实例单 server, 多语言需多实例 | **MVP 唯一推荐**; 但 API 偏 IDE 风格, agent 化包装需自己写 |
| **pylspclient** | [yeger00/pylspclient](https://github.com/yeger00/pylspclient) | 141 | 2025-06 (近一年仅 dependabot) | 简洁 Content-Length 实现 ([lsp_endpoint.py](https://github.com/yeger00/pylspclient/blob/main/pylspclient/lsp_endpoint.py)) | 基础请求/通知抽象, 不管 server 下载, 不管 capability negotiation | 支持但需要手工 register | 不管 | 适合学习 / 自己造轮子的底层 transport; 不建议直接用 |
| **sansio-lsp-client** | [PurpleMyst/sansio-lsp-client](https://github.com/PurpleMyst/sansio-lsp-client) | 29 | 2025-05 | 是 (sans-io: 自己接 stdio) | 完整事件机器 | server-initiated event 用 enum 派发 | 调用方自己管 | 教科书设计但生态太小, 仅 Porcupine 编辑器在用 |
| **solidlsp** (Serena fork) | [oraios/serena `src/solidlsp/`](https://github.com/oraios/serena/tree/main/src/solidlsp) | 24677 (主仓) | 2026-05-27 | Content-Length, 源头同 multilspy/OLSP | initialize + workspace folder + capability + 自动 publishDiagnostics 缓存 + pull diagnostics + 进程崩溃恢复 | 内置 `textDocument/publishDiagnostics` 监听 + workspace/configuration 响应 | **是, `LanguageServerManager` 并行起多个 server**([ls_manager.py](https://github.com/oraios/serena/blob/main/src/serena/ls_manager.py)) | 想"抄作业"就抄这个; 但版权 MIT, 注意它 import 了 `sensai` / `overrides` 等若干小依赖 |
| **pygls** | [openlawlibrary/pygls](https://github.com/openlawlibrary/pygls) | 798 | 2026-05-22 | -- | -- | -- | -- | **server-side** 框架, 不是 client; 仅参考 |

### 关键判断

- **multilspy 共享上游与 solidlsp 同源**: 两者的 `lsp_protocol_handler/server.py` 头部都注明 "obtained from [predragnikolic/OLSP](https://github.com/predragnikolic/OLSP) under MIT License". 也就是说选 multilspy = 选了 Serena/OLSP 那条经过实战的 JSON-RPC + Content-Length transport.
- **multilspy 的杀手特性**: 内置 `language_servers/` 下 12 个 server 的"自动下载 jdtls / 自动 pip install jedi / 自动 npm install typescript-language-server"逻辑. BareAgent 自己实现需要花相当时间, 直接用可省至少一周.
- **multilspy 短板**: 单 instance 单语言 (`MultilspyConfig(code_language="python")`); 同一个项目里要同时支持 py + ts 就得跑两份 `SyncLanguageServer` 实例. Serena 的 `LanguageServerManager` 就是为补这个洞写的.
- **pylspclient 几乎冻结**: 2025-06 后 commit 全是 dependabot 自动 bump 依赖, 没有 feature work. 选它意味着 transport 层 bug 全自己修.

---

## 2. AI Agent 暴露 LSP 的三种模式

### 模式 A: 纯工具化 (explicit tool calls)

LLM 直接调 `lsp_definition(file, line, col)` / `lsp_references(...)` / `lsp_diagnostics(file)`. 模型决定调用时机和参数.

- **Serena**: [src/serena/tools/symbol_tools.py](https://github.com/oraios/serena/blob/main/src/serena/tools/symbol_tools.py) 暴露 13 个 LSP 类工具. 真实工具名:
  - `GetSymbolsOverviewTool` (`textDocument/documentSymbol`)
  - `FindSymbolTool` (`workspace/symbol` + 内置 name-path 解析)
  - `FindReferencingSymbolsTool` (`textDocument/references`)
  - `FindImplementationsTool` (`textDocument/implementation`)
  - `FindDeclarationTool` (`textDocument/declaration`)
  - `GetDiagnosticsForFileTool` / `GetDiagnosticsForSymbolTool`
  - `ReplaceSymbolBodyTool` / `InsertAfterSymbolTool` / `InsertBeforeSymbolTool` (编辑工具, 走 LSP range)
  - `RenameSymbolTool` (`textDocument/rename`)
  - `SafeDeleteSymbol`
  - `RestartLanguageServerTool`
- **Cline SDK 示例插件**: [sdk/examples/plugins/typescript-lsp/index.ts](https://github.com/cline/cline/blob/main/sdk/examples/plugins/typescript-lsp/index.ts) — 极简化到只有一个 `goto_definition(file, line)`. 注: 这个 plugin 实际用的是 TypeScript Compiler API 而不是 raw LSP, 但 agent 接口形态是 LSP 风格的.
- **优点**: 模型显式调用, 行为可解释 / 可审计; LSP 调用控制权完全在 LLM, 不浪费 token.
- **缺点**: 模型可能忘记调; tool description 写得不好, LLM 不知道什么时候用.

### 模式 B: 隐式上下文 (implicit context injection)

用户/agent 读/编辑文件时, 自动把诊断 + 相关符号信息注入到 system prompt 或下一轮 user-turn.

- **Cline / Roo-Code**: [apps/vscode/src/integrations/diagnostics/index.ts](https://github.com/cline/cline/blob/main/apps/vscode/src/integrations/diagnostics/index.ts) `getNewDiagnostics()` 做 before/after diff, 只把"新增的" diagnostic 拼成纯文本喂给 LLM. 比如:
  ```
  src/foo.ts
  - [tsc Error] Line 12: Property 'bar' does not exist on type 'Foo'.
  ```
  通过 `environmentDetails` 块自动附加, LLM 完全不需要调工具.
- **Continue.dev**: [extensions/vscode/src/autocomplete/lsp.ts](https://github.com/continuedev/continue/blob/main/extensions/vscode/src/autocomplete/lsp.ts) 用 `vscode.commands.executeCommand("vscode.executeDefinitionProvider", ...)` 在补全前预解析光标处符号定义并拼进 prompt — 用户感知不到, 都是后台.
- **优点**: 模型 always sees fresh state, 不会"忘了看错误"; 用户体验最自然.
- **缺点**: token 开销可能爆掉(项目大 → 全量诊断巨多); 何时该注入难掌控; 调试时很难看清"模型到底看到了什么".

### 模式 C: Hybrid (主流真实方案)

读类工具显式化, 写类工具自动伴随诊断 diff 注入.

- **Serena**: `EditingToolWithDiagnostics` 基类([tools_base.py](https://github.com/oraios/serena/blob/main/src/serena/tools/tools_base.py)) 在编辑前后各 snapshot 一次 `publishDiagnostics`, 然后把 diff 拼进 tool result. 当前默认 `ENABLE_DIAGNOSTICS = False` (作者评注: 单步编辑常会引入临时错误, noisy), 但 hook 已就位.
- **Cline**: write_to_file/replace_in_file 这两个 tool 的 result 后面强制附 "Now you have the latest diagnostics for ...". 实测 Claude/GPT 都很会用.
- **典型项目**:
  - [oraios/serena](https://github.com/oraios/serena) -- 24.7k★, MCP 形态, 见 `src/serena/tools/`
  - [cline/cline](https://github.com/cline/cline) -- 62k★, VSCode 内 -- `apps/vscode/src/integrations/diagnostics/index.ts`
  - [RooCodeInc/Roo-Code](https://github.com/RooCodeInc/Roo-Code) -- Cline fork, 同样 hybrid 风格

### 选择标准

| 你的 agent 是... | 选什么 |
|---|---|
| IDE 插件 (VSCode/JetBrains 内部跑) | 模式 B; 借宿主的 `executeDefinitionProvider` 等命令 |
| 独立 CLI agent (BareAgent / Aider / Codex CLI) | 模式 C; 自己起 LSP 进程, hybrid 暴露 |
| MCP server 给别人当工具用 | 模式 A; 让 host 决定何时调 |

---

## 3. LSP 方法的 AI Agent 价值密度排序

基于 Serena 实际暴露 + Cline plugin 的极简化版本:

### Tier 1 -- MVP 必做

| LSP 方法 | Agent tool 名 | 为什么 |
|---|---|---|
| `textDocument/documentSymbol` | `outline(file)` / `get_symbols_overview` | LLM 看新文件第一步, 比读全文便宜 10x. Serena `GetSymbolsOverviewTool` 也明确说 "first tool to call when you want to understand a new file" |
| `textDocument/definition` | `find_definition(file, line, col)` | 替代 grep, 跨 re-export / import 准 |
| `textDocument/references` | `find_references(file, line, col)` | 重构/影响面分析必备 |
| `textDocument/publishDiagnostics` (push) + `textDocument/diagnostic` (pull, LSP 3.17+) | `get_diagnostics(file)` + 自动注入 | 写后验证, 闭环修复. **必须同时支持 push 和 pull**: pyright/rust-analyzer 推, tsserver/clangd 老版本只推, 新版可拉. Serena 双轨实现见 [solidlsp/ls.py: `request_text_document_diagnostics` & `request_published_text_document_diagnostics`](https://github.com/oraios/serena/blob/main/src/solidlsp/ls.py) |

### Tier 2 -- v2 加分项

| LSP 方法 | 价值 | 备注 |
|---|---|---|
| `textDocument/hover` | 类型签名 / docstring | 信息密度好但和 `documentSymbol` 重叠不少 |
| `workspace/symbol` | 全局模糊符号搜索 | 大项目导航很有用; Serena 的 `FindSymbolTool` 就是这个 + 后处理 |
| `textDocument/implementation` | 找接口实现 | 对 Java/C# 项目 ROI 高 |
| `textDocument/rename` (`prepareRename` + workspace edit) | 安全重命名 | LLM 自己改名极容易漏 1-2 处; 但 LSP 返回的 WorkspaceEdit 处理逻辑复杂(多文件 + version 校验) |

### Tier 3 -- 在 LLM 循环里 ROI 低, 可不做

| LSP 方法 | 为什么不推荐 |
|---|---|
| `textDocument/completion` | 设计给人打字 streaming 用; LLM 自己生成代码不需要 server 帮提示 |
| `textDocument/signatureHelp` | 同上, hover 已经够了 |
| `textDocument/codeAction` | 返回的 action 列表本身需要二次决策, latency 翻倍; 大部分修复 LLM 自己能搞 |
| `textDocument/formatting` / `rangeFormatting` | 让 LLM 调 `prettier` / `black` subprocess 更简单 |
| `textDocument/foldingRange` / `selectionRange` | UI 类, agent 不需要 |
| `callHierarchy` / `typeHierarchy` | 价值高但实现复杂, 多数 server 还不支持, v3 再说 |

### Diagnostics 消费的关键决策

LSP 3.17(2022 发布) 增加了 `textDocument/diagnostic` (pull). 现状(2026):

- **必须支持 push**: pyright / rust-analyzer / tsserver / gopls 都还以 `publishDiagnostics` 为主.
- **应该支持 pull**: 适合 agent 编辑后"立刻知道结果"的场景, 不用等 server 推. Serena 的做法是 `request_text_document_diagnostics()` 先尝试 pull, 失败回退到等待 cached push.
- **关键 race condition**: 编辑后立刻 pull diagnostics 时, server 可能还没分析完. Serena 用 `analysis_complete = threading.Event()` + `request_published_text_document_diagnostics(after_generation=N, timeout=2.5)` 解决, 见 [pyright_server.py](https://github.com/oraios/serena/blob/main/src/solidlsp/language_servers/pyright_server.py).

---

## 4. 多语言 server 启动 / 协调实践

行业里只有 Serena 把这件事做完整了, 其它 agent 要么单语言要么靠 IDE 宿主.

### Serena 的做法 ([ls_manager.py](https://github.com/oraios/serena/blob/main/src/serena/ls_manager.py))

- **检测**: 项目启动时 scan 文件扩展名, 决定要启哪些 server.
- **并行启动**: `LanguageServerManager.from_languages()` 给每个语言开一个 `StartLSThread`, 全部 `thread.join()` 后再返回.
- **Fail-fast**: 任一 server 起不来就 stop 已起的, 抛 `LanguageServerManagerInitialisationError`. 作者评注: "better to make symbolic tool calls fail than silently continue with subset".
- **进程隔离**: 每个 server 是独立子进程, 通过 `StdioLanguageServer`(`solidlsp/ls_process.py`) 抽象 stdin/stdout. 崩溃恢复靠 `RestartLanguageServerTool` 给 LLM 自己调.
- **资源**: 启 server 的时候有 `ls_timeout`; pyright 启动 + 全项目索引一般 5-15s, jdtls 30-90s(项目大可能更慢).

### 文件 → server 路由

- multilspy: 一个实例 = 一个语言, 调用者自己判断哪个文件该问哪个实例.
- Serena: `SolidLanguageServer.can_analyze_file(path)` 由每个 server adapter 实现 (基于扩展名 + filename matcher); 工具拿到 `relative_path` 后让 `LanguageServerManager` 自动派发到正确 server. 见 `symbol_retriever.py` -> `get_language_server(path)`.

### 启动期与 LLM 的协调

- pyright 启动到 "ready" 之间, 模型若提前提问会拿到空结果. Serena 在 `PyrightServer.__init__()` 里设置 `analysis_complete = threading.Event()`, 监听 progress notifications 才 set.
- jdtls 启动很慢 -- multilspy 直接在 README 里要求 JDK 17+ 并预下载 jdtls.

### 进程资源占用

- pyright + rust-analyzer + tsserver 同时跑, **每个 ~ 200-500MB RSS**. multilspy 没做限流; Serena 通过 `LanguageServerManager` 串行管理生命周期, 用户随时可 `RestartLanguageServer`.

---

## 5. 对 BareAgent 的建议

### 当前架构梳理

BareAgent 已有: MCP transport (`src/mcp/`)、工具系统 (`src/core/tools.py` 区分 `BASE_TOOLS` / `DEFERRED_TOOLS`)、subagent + 权限隔离 (`src/planning/subagent.py` + `src/permission/guard.py`)、background runner (`src/concurrency/background.py`). 没有 LSP, 没有 tree-sitter repo-map.

### MVP 建议范围 (一个 PR, 1-2 周)

1. **依赖**: 引入 `multilspy` (PyPI 发布的稳定版本); 不要直接拷 Serena 的 solidlsp 进来(版权 + 维护拖累). 但 **架构上抄 solidlsp**: 把 multilspy 的 `SyncLanguageServer` 包一层 `LanguageServerManager` (多语言并行启动, 文件路径 -> server 路由).
2. **工具集**: 新增延迟加载工具组 `LSP_TOOLS`, 含 4 个 tool, 全部仅在用户/PRD 显式启用时注入(避免拖慢冷启动):
   - `lsp_outline(file)` -- `documentSymbol`
   - `lsp_definition(file, line, col)`
   - `lsp_references(file, line, col)`
   - `lsp_diagnostics(file)` -- 优先 pull, 回退 push cache
3. **隐式注入(可选)**: `edit_file` / `write_file` handler 成功后, 如果 LSP 已就绪, 在 tool result 末尾追加 diff 新增的 diagnostics (复用 Cline 的 `getNewDiagnostics` 思路, 不显示已存在 issue). 默认 OFF, 配置开关.
4. **生命周期**: LSP server 进程当作 BareAgent 全局单例; 进程退出时统一 `shutdown` + `exit`. 用 `BackgroundManager` 跑启动以免阻塞 REPL.
5. **权限**: 把 LSP 工具按 explore 类型对待 (`AgentType` 加进 `explore` 白名单); 写类操作(rename) 保留给 default mode, 不进 plan/explore 类型.

### v2 增量

- 增加 `lsp_hover` / `lsp_workspace_symbol` / `lsp_rename`.
- 多 server 并行(目前 BareAgent 同时支持 pyright + rust-analyzer + tsserver 是合理目标).
- Diagnostics pull/push 双轨完整实现.

### 跳过项

- 不做 `completion` / `signatureHelp` / `codeAction` / `formatting`.
- 不自己造 LSP transport(Content-Length / JSON-RPC), 让 multilspy 处理.
- 短期内不和 Aider 风格 tree-sitter repo-map 竞争 -- 这是不同 niche, LSP 准但慢, tree-sitter 快但不精确.

### 注意事项

- `multilspy` 的依赖会拉 `tree-sitter-languages` + 各种 language-specific runtime helpers. 把它放在可选 extra 里 (类似项目里的 `[langfuse]`): `uv pip install -e ".[lsp]"`.
- multilspy 要求 Python >= 3.10, BareAgent 已经是 3.12+, 没问题.
- Windows 路径处理是 LSP 常见坑(URI 编码 vs Windows backslash); multilspy 已经处理了, 但任何自写代码都要走它的 `PathUtils`.
- 多语言切换时 jdtls 可能起 60s+; UX 上要么后台启动 + 显示 "Java LSP starting" 提示, 要么让用户在 config 里显式声明项目主语言.

---

## Caveats / Not Found

- **Cursor 源码闭源**, 只能从其文档和反编译猜: 它走 IDE 嵌入路线, 但具体是不是直接复用 VSCode `executeCommand` 通道还是另起 LSP 进程不确定. 未能找到一手代码.
- **Claude Code 的 IDE 集成模式**: Anthropic 官方文档只说"VSCode/Cursor IDE 插件存在", 没有公开 LSP 集成细节. 推测同 Cline 路径.
- **Codeium / Tabby**: 主要做 completion, 暴露给 LLM 的工具调用模式没有公开 design doc 可引用.
- **Cody (Sourcegraph)**: API 限速, 没拿到 stars 数据; 历史上是基于他们自家 Scip indexer 而不是 LSP. 跟本研究的"agent 怎么用 LSP" niche 不同.
- **本报告所有结论基于 README + 主要源文件 spot check**, 没有跑真实 benchmark 比较 multilspy vs solidlsp 性能差异. 如果 BareAgent MVP 落地后发现 multilspy 性能不够, 再评估是否 fork.
