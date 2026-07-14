# 执行计划：vision 能力门控 + 终端图片输入

有序两阶段，① 先落地（是 ② 的消费方前置）。每阶段自带验证；全部完成跑 CI 四步。

## 阶段 A — ① vision 能力门控（read 侧）

### A1. 能力模块
- [ ] 新建 `src/bareagent/provider/capabilities.py`：`KNOWN_IMAGE_INPUT_PREFIXES` + `supports_image_input(model, *, override=None)`（override 优先 / 前缀命中 / 未知 deny）。加 video/audio 扩展位注释。
- [ ] 新建 `tests/test_capabilities.py`：各家前缀命中、未知 deny、override True/False 优先级。

### A2. read_file 运行时门控
- [ ] `src/bareagent/core/handlers/file_read.py`：`run_read` 加 `image_enabled: bool = True`；图片分支 False 时返 `_IMAGE_DISABLED_ERROR`（模块级常量文案，含 config/env 覆盖提示）。
- [ ] `tests/test_file_read.py`：补 `image_enabled=False → Error` / `=True → blocks` 两条。

### A3. 配置
- [ ] `src/bareagent/main.py`：`CapabilitiesConfig(image_in: bool | None = None)` + `_parse_capabilities_config`（容错、env `BAREAGENT_MODEL_IMAGE_IN` 覆盖、缺省保持 None）；`Config` 加 `capabilities` 字段（尾部 defaulted）；`load_config` 接线解析 `[capabilities]`。
- [ ] `config.toml` 加 `[capabilities]` 段（注释说明 image_in 缺省=auto/查表）。
- [ ] 确认 `capabilities.*` **不**进 `_HOT_RELOAD_PATHS`（restart-required）；如仓库有 reload guard 测试，确认其 diff 归类正确。

### A4. handler 接线
- [ ] `src/bareagent/core/tools.py`：`get_handlers` 加 `image_input_enabled: bool = True`，`read_file` partial 绑 `image_enabled`；`rebind_workspace_handlers`（worktree 重绑）从原 partial `.keywords` 取回 `image_enabled` 复用。
- [ ] `src/bareagent/main.py:_build_handlers`：算 `supports_image_input(provider.model, override=config.capabilities.image_in)` 并透传 `image_input_enabled`。

### A 验证
- [ ] `uv run pytest tests/test_capabilities.py tests/test_file_read.py -q`
- [ ] 手测（可选）：非 vision 模型 `read_file` 读 png 返 Error；vision 模型正常。

## 阶段 B — ② 终端图片输入（write 侧）

### B1. 附件纯逻辑模块
- [ ] 新建 `src/bareagent/ui/attachments.py`：`extract_attachments` / `build_attachment_prefix` / `grab_clipboard_image`（Pillow lazy import, fail-open）。
- [ ] 新建 `tests/test_attachments.py`：占位符解析（多/保序去重/无）、前缀构造（空/多）、剪贴板抓取（monkeypatch ImageGrab：Image / 路径列表 / None / 异常）。

### B2. prompt.py Ctrl-V
- [ ] `src/bareagent/ui/prompt.py`：`AgentPrompt` 加 `on_paste: Callable[[], str|None] | None = None`；`c-v` 绑定调 `on_paste`，返回串则 `insert_text(f"[image:{rel}]")`，None 静默。

### B3. main.py 接线
- [ ] `_build_stdio_read_fn`：建 `.bareagent_attachments/`（fail-open）、`itertools.count`、`on_paste` 闭包（调 `grab_clipboard_image` → 相对串 / None + 提示），传进 `AgentPrompt`；传 `ui_console` 以打提示。
- [ ] `/attach <path>` 命令：纯函数 `_handle_attach_command`（校验图片扩展名 + 存在；workspace 内直引 / 外部 `shutil.copy`；返回相对串 + feedback）；REPL 分支登记进 `pending_attachments`、`print_status`、`continue`；登记 `_SLASH_COMMANDS` + `_HELP_TEXT`。
- [ ] `pending_attachments: list[str]` 会话级；在 /new /clear /resume /import /fork 切换点 `.clear()`（镜像 `pending_team_messages`）。
- [ ] 提交拼装（line ~4513 之后、`messages.append` 之前）：`extract_attachments(text)` + 合并 `pending_attachments` → `build_attachment_prefix` → 前缀置最前 → `pending_attachments.clear()`。

### B4. 依赖
- [ ] `pyproject.toml`：`[project.optional-dependencies] clipboard = ["Pillow>=10"]`。
- [ ] README 补一行：粘贴/attach 用法 + 建议 gitignore `.bareagent_attachments/`。

### B5. 命令拼装纯函数测试
- [ ] `tests/test_attachments.py` 或 `test_attach_command.py`：`_handle_attach_command`（内引/外部 copy/非图/不存在）+ 前缀合并顺序。

### B 验证
- [ ] `uv run pytest tests/test_attachments.py -q`
- [ ] 手测（可选，需 tty + Pillow）：Ctrl-V 粘贴插占位符 → 提交 → 模型 read_file 看到图；`/attach 外部图` → 复制 + 前缀注入。

## 收尾（全量）

- [ ] `bash scripts/ci-check.sh`（ruff check / ruff format --check / pyright / uv run pytest）全绿。
- [ ] 更新 `CLAUDE.md` 架构小节（新增能力门控 + 终端图片输入两段，仿现有小节密度）。
- [ ] Conventional Commits 提交（`Feat:`）；提交信息用文件 + `git commit -F`（避免 here-string 坑）。

## 回滚点

- 阶段 A 独立可回滚（纯新增 + 默认参数向后兼容，撤回 `image_input_enabled` 透传即恢复）。
- 阶段 B 独立于 A（B 只依赖 A 已落地的 read_file 门控作为消费方；撤回 B 不影响 A）。

## 验证命令汇总

```bash
uv run pytest tests/test_capabilities.py tests/test_file_read.py tests/test_attachments.py -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
bash scripts/ci-check.sh
```
