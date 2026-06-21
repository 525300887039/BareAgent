# Workspace Index - ducat

> Journal tracking for AI development sessions.

---

## Current Status

<!-- @@@auto:current-status -->
- **Active File**: `journal-1.md`
- **Total Sessions**: 49
- **Last Active**: 2026-06-21
<!-- @@@/auto:current-status -->

---

## Active Documents

<!-- @@@auto:active-documents -->
| File | Lines | Status |
|------|-------|--------|
| `journal-1.md` | ~1653 | Active |
<!-- @@@/auto:active-documents -->

---

## Session History

<!-- @@@auto:session-history -->
| # | Date | Title | Commits | Branch |
|---|------|-------|---------|--------|
| 49 | 2026-06-21 | 会话 fork 与树状分支 (/fork + /tree) | `0e373dd`, `07100a1` | `feat/session-fork` |
| 48 | 2026-06-21 | pyright 类型门收紧到 standard 模式 | `eb05c9f`, `3618287` | `main` |
| 47 | 2026-06-21 | pyright 类型检查接入 CI（配了却没强制执行的门） | `ddd6ad5`, `51652a7`, `f621859` | `main` |
| 46 | 2026-06-21 | CI windows-latest matrix：覆盖开发主力平台 | `e5eedaf`, `e8ed899` | `main` |
| 45 | 2026-06-20 | CI socket job：捡回 localhost-socket 测试零覆盖 + 纳入 main 变红通知 | `9b5dc99`, `f5118a9` | `main` |
| 44 | 2026-06-20 | CI 可见性：pre-push 本地闸 + main 变红通知 | `e09ace3`, `c4846c2` | `feat/ci-visibility` |
| 43 | 2026-06-20 | Repo Map: 符号骨架 + 结构全景工具 | `328413a`, `e1430cb`, `108df47`, `b2fa3d8`, `6b38bf9` | `feat/repo-map` |
| 42 | 2026-06-20 | 语义代码检索 code_search: 复用 embedding 层 + boot 门控工具 | `826d25a`, `92534f0`, `078528f` | `feat/semantic-code-search` |
| 41 | 2026-06-19 | 多 provider 缓存统一抽象层: CacheEconomics + cache_mode + Gemini preset | `4590e7c`, `97c02f6` | `feat/cache-abstraction-layer` |
| 40 | 2026-06-19 | 省 token 三件套: GPT-5 缓存倍率 + anchor 断点 + grep output_mode | `49cd034`, `f6d27e6`, `38d7ff0` | `feat/token-saving-trio` |
| 39 | 2026-06-14 | 文档同步 src-layout 路径 + 补 PyPI 安装方式 | `2a93ece` | `main` |
| 38 | 2026-06-14 | PyPI 打包 bareagent-cli + tag 触发 Trusted Publishing 自动发布 | `113bc16`, `c2b7de3` | `feat/pypi-tag-ci` |
| 37 | 2026-06-08 | Workflow 后台执行 + /workflows 面板 + resume + token budget | `f5aba62` | `main` |
| 36 | 2026-06-08 | 语义/向量记忆召回（可插拔 embedding backend + 词法回退） | `7799a0d` | `main` |
| 35 | 2026-06-08 | team 收口：team_register 动态建队友 + PLAN_APPROVAL 发送侧接线 | `d7dccc8` | `main` |
| 34 | 2026-06-08 | provider 空响应诊断（completed 但 text+tool 皆空时非致命 warn） | `a980bcd` | `main` |
| 33 | 2026-06-08 | team 队友有状态记忆（跨 request 对话续跑 + per-teammate Compactor） | `8dcfbe8` | `main` |
| 32 | 2026-06-06 | 子代理 SendMessage 续跑 | `279e20b` | `main` |
| 31 | 2026-06-06 | 完善 team/AutonomousAgent 子系统（回路闭合 + 异常隔离 + team_shutdown + 活性检测 + 提速） | `e0e3c00` | `main` |
| 30 | 2026-06-06 | Workflow 确定性编排（声明式 DAG + 并行 fan-out） | `fd179ff` | `main` |
| 29 | 2026-06-06 | goal 完成条件循环（/goal） | `5d13562` | `main` |
| 28 | 2026-06-05 | Plan 模式工作流（exit_plan_mode 呈递审批 + 转执行） | `f4e13ae` | `main` |
| 27 | 2026-06-01 | skill 自进化 (agent 更新已有 skill) | `d631ea6`, `2cc9265` | `main` |
| 26 | 2026-06-01 | 经验式技能生成 (agent 从经验自动长 skill) | `140e0f2`, `f60686d` | `main` |
| 25 | 2026-06-01 | ROADMAP Prompt Caching (Anthropic) | `cbfcc66`, `79a9638` | `main` |
| 24 | 2026-06-01 | 对话导入导出 /export + /import | `1e8e477` | `main` |
| 23 | 2026-06-01 | ROADMAP 4.3 配置热重载 | `953daad` | `main` |
| 22 | 2026-06-01 | ROADMAP 4.2 LLM 重试策略 | `2046332` | `main` |
| 21 | 2026-06-01 | Cron 定时任务调度与 /loop 命令（ROADMAP 4.1） | `7970881` | `main` |
| 20 | 2026-06-01 | Git Worktree 子代理隔离（ROADMAP 3.3） | `bd49e2d` | `main` |
| 19 | 2026-06-01 | 代码审查修复（WorkspaceEdit 双形态/UTF-16 偏移/PDF 页范围越界） | `1c4a04b` | `main` |
| 18 | 2026-06-01 | Hooks 系统（PreToolUse/PostToolUse 工具调用钩子） | `f79716f` | `main` |
| 17 | 2026-06-01 | 本地多模态文件读取（图片/PDF/notebook） | `291a12b` | `main` |
| 16 | 2026-06-01 | Token 用量追踪与成本展示（/cost 命令） | `e6f9589` | `main` |
| 15 | 2026-06-01 | 语义重命名工具 semantic_rename（基于 LSP textDocument/rename） | `bf700ed` | `main` |
| 14 | 2026-05-31 | 修复 bash 工具 Windows 中文输出乱码（GBK→UTF-8） | `64f535a`, `35e85c1` | `main` |
| 13 | 2026-05-31 | web_search 改用 Bing HTML 抓取（免 key 免费） | `49b8a8e`, `1ab14d6` | `main` |
| 12 | 2026-05-30 | 交互式初始化向导 bareagent init（多 provider 配置） | `55da9c8`, `7fd0e85` | `main` |
| 11 | 2026-05-30 | 持久化记忆系统（文件式 agent 记忆 + 召回层） | `9216b78` | `main` |
| 10 | 2026-05-30 | 工程化护栏修复 (健康体检收尾 T1-T4) | `b568073` | `main` |
| 9 | 2026-05-28 | LSP child B: 集成 + UX + E2E + 文档（LSP 大任务收尾） | `776b7f5` | `main` |
| 8 | 2026-05-28 | LSP child A: src/lsp/ 骨架 + 4 工具 + agent_types 集成 | `3b427aa` | `main` |
| 7 | 2026-05-27 | PR6: MCP 生命周期硬化 + E2E + 文档（收尾） | `ebb1f3c` | `main` |
| 6 | 2026-05-27 | PR5: MCP 多模态结果回传 + provider 适配 | `b8da7b7` | `main` |
| 5 | 2026-05-27 | PR4: MCP 权限 + 子代理隔离 + REPL 命令 | `ba7d0f5` | `main` |
| 4 | 2026-05-27 | PR3: MCP Resources + Prompts 支持 | `6ea295e` | `main` |
| 3 | 2026-05-27 | PR2: MCP Client + Manager + tools 注入 | `1c84fa8` | `main` |
| 2 | 2026-05-27 | MCP 客户端规划 + PR1 transport/protocol 落地 | `b9f64ff`, `2c57281`, `96fc962`, `deb27bb` | `main` |
| 1 | 2026-05-27 | 接入 trellis 并完成 bootstrap 规范填充 | `7b15cb5`, `2e9e6e4`, `1aa668c`, `3fa5e52` | `main` |
<!-- @@@/auto:session-history -->

---

## Notes

- Sessions are appended to journal files
- New journal file created when current exceeds 2000 lines
- Use `add_session.py` to record sessions