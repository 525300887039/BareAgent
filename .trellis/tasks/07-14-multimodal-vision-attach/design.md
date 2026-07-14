# 技术设计：vision 能力门控 + 终端图片输入

## 一、边界与总体数据流

```
① 门控 (read 侧)
  provider.model + config.capabilities.image_in
        │ _build_handlers 计算 bool
        ▼
  get_handlers(image_input_enabled=bool)
        │ partial 绑定
        ▼
  run_read(..., image_enabled=bool)
        │ 图片扩展名分支：False → 友好 Error；True → [text, image] blocks（现状不变）

② 输入 (write 侧)
  /attach <path>  ──copy──►  workspace/.bareagent_attachments/  ──登记──► pending_attachments[list]
  Ctrl-V 粘贴 ──grab(Pillow)──► 同目录 ──插入──► 输入框 "[image:相对路径]"
        │ 提交
        ▼
  extract_attachments(text) 解析占位符 + pending_attachments 合并
        │ build_attachment_prefix
        ▼
  前缀 "用户提供了图片 <rel>，请用 read_file 查看" 拼进 user text（复用 _prepend_pending_context 位）
        ▼
  模型调 read_file(<rel>) → 走 ① 门控后的 read 路径
```

关键：② 产出的是 workspace 相对路径，被 ① 门控的 `read_file` 消费——两子目标在 `read_file` 处汇合，① 必须先落地。

## 二、① vision 能力门控

### 2.1 为什么不用 provider `ClassVar`（偏离 `cache_mode` 范式的理由）

`cache_mode` 是 provider ClassVar 因为缓存能力是 **provider 通道级**事实（Anthropic explicit / OpenAI auto）。而 vision 是 **模型级**属性：同一 `OpenAIProvider` 既服务 `gpt-4o`（vision）又服务 `deepseek-chat`（纯文本）。故 image_in 应是"模型 → 能力"的静态查表（presets.py 静态 dict 的精神），不是 provider ClassVar。设计遵循任务要求的"静态声明 + config/env 覆盖"精神，载体换成模型前缀表。

### 2.2 新模块 `src/bareagent/provider/capabilities.py`（纯逻辑，可单测）

```python
# 已知支持图片输入的模型 id 前缀（保守收录，跨 provider）。
KNOWN_IMAGE_INPUT_PREFIXES: tuple[str, ...] = (
    # Anthropic：Claude 3 起全系 vision
    "claude-3", "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
    "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-8", "claude-sonnet-4-6",
    # OpenAI：4o / 4.1 / 4-turbo / 4-vision / o-系推理带视觉
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-4-vision", "gpt-5",
    "o1", "o3", "o4",
    # Gemini：2.x 全系多模态
    "gemini-1.5", "gemini-2",
    # Qwen-VL / GLM-4V / DeepSeek-VL 等显式视觉型号
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "glm-4v", "glm-4.6v", "deepseek-vl",
)

def supports_image_input(model: str, *, override: bool | None = None) -> bool:
    """模型是否接受图片输入。override(True/False) 优先；否则前缀表命中；
    未知模型默认 False（fail-safe，杜绝撞 API）。"""
    if override is not None:
        return override
    m = (model or "").strip().lower()
    return any(m.startswith(p) for p in KNOWN_IMAGE_INPUT_PREFIXES)
```

- **未知默认 deny**：只有确认的 vision 模型放行，从根上杜绝"撞 API"。代价（新 vision 模型需配置/补表）由友好 Error 文案化解。
- 前缀表是"活的知识"，会随模型演进漂移——同 `token_tracker.DEFAULT_PRICES` 的注释精神：表可能过期，权威兜底是 config/env 覆盖。
- 模块只暴露 `image_in` 一个 kind。留 `# 扩展位：video_in/audio_in 后续加` 注释，不建枚举脚手架（YAGNI）。

### 2.3 覆盖来源与优先级

`env BAREAGENT_MODEL_IMAGE_IN > config [capabilities] image_in > 静态表`

- 新 `CapabilitiesConfig`（main.py，`slots=True`）：`image_in: bool | None = None`（None = auto/查表）。
- `_parse_capabilities_config(raw)`：逐字段容错——`image_in` 走 `_resolve_bool`-式解析但**允许缺省保持 None**（不是 bool 默认）；env `BAREAGENT_MODEL_IMAGE_IN` 存在则覆盖成显式 bool，不存在保留 config 值（含 None）。非法值回退 None。不崩 boot。
- `Config` 加 `capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)`（尾部 defaulted，不破坏现有构造）。
- **restart-required**（不进 `_HOT_RELOAD_PATHS`）：能力在 boot 建 handlers 时固化，同 provider。

### 2.4 门控注入点（read_file 运行时门控，不 boot 隐藏）

- `run_read` 加参数 `image_enabled: bool = True`（默认 True → 现有调用/测试字节级不变）。图片分支：
  ```python
  if suffix in _IMAGE_EXT_TO_MIME:
      if not image_enabled:
          return _IMAGE_DISABLED_ERROR  # 友好 Error 字符串
      return _read_image(resolved, _IMAGE_EXT_TO_MIME[suffix])
  ```
  Error 文案（不含 model 名，run_read 是 model-agnostic）：
  `"Error: the current model has no image (vision) capability, so this image was not sent to avoid an API error. Switch to a vision-capable model, or set [capabilities] image_in = true (or env BAREAGENT_MODEL_IMAGE_IN=1) if this model does support images."`
- `get_handlers` 加 `image_input_enabled: bool = True`，`read_file` partial 绑定：`partial(run_read, workspace=workspace, image_enabled=image_input_enabled)`。注意 `_with_recency` 包装在其后，wrapper 透传 kwargs 不受影响。
- `_build_handlers` 计算并透传：
  ```python
  image_input_enabled = supports_image_input(
      getattr(provider, "model", "") or "",
      override=config.capabilities.image_in,
  )
  ```
  子代理经同一 `_build_handlers`/`get_handlers`（同 provider）建 handlers → 天然继承同门控。worktree `rebind_workspace_handlers` 重绑 read_file（tools.py:902）时也需带上 `image_enabled`——重绑处从原 partial 的 `.keywords` 取回 `image_enabled` 复用（同它取 `diagnostics_hook` 的手法），避免隔离子代理丢门控。

### 2.5 为何不像 code_search 那样 boot 门控

`read_file` 是文本/PDF/notebook/图片统一入口，无法因"无 vision"就整体隐藏（会连读文本都没了）。故对齐 kimi-cli 的另一半——**运行时按 kind 二次校验回友好错误**。这是 read_file 多用途性质决定的刻意偏离，非遗漏。

## 三、② 终端图片输入

### 3.1 附件落盘位置（硬约束驱动）

`sandbox.safe_path` 拒绝绝对路径 + 限定 workspace 内（sandbox.py:8-16）。故附件必须落 workspace 内、以相对路径引用：
- 固定目录 `workspace/.bareagent_attachments/`（不做 per-session 子目录——AgentPrompt 只建一次，per-session 会增加会话切换 churn；ponytail：单目录 + 唯一文件名，不自动清理，boot 时可选清空）。
- 文件名唯一：`attach-<counter>-<basename>` 或 `paste-<counter>.png`（计数器在 AgentPrompt/主循环持有；避免 `Date.now`-式依赖，用递增计数）。
- 相对路径 = `.bareagent_attachments/<name>`，喂给 `read_file` 恰好 workspace-relative，safe_path 通过。
- 建议在 README/提示里让用户 gitignore `.bareagent_attachments/`（不强改用户 .gitignore）。

### 3.2 新模块 `src/bareagent/ui/attachments.py`（纯逻辑，可单测，无 prompt-toolkit/main 依赖）

```python
_MARKER_RE = re.compile(r"\[image:([^\]]+)\]")

def extract_attachments(text: str) -> tuple[str, list[str]]:
    """从提交文本剥离 [image:<相对路径>] 占位符，返回 (去占位符文本, 路径列表, 保序去重)。"""

def build_attachment_prefix(paths: list[str]) -> str:
    """把相对路径列表构造成前缀行：
    每条 '用户提供了图片 <path>，请用 read_file 查看'，多图多行；空列表返回 ""。"""

def grab_clipboard_image(dest_dir: Path, name: str) -> Path | None:
    """lazy import PIL.ImageGrab。抓剪贴板：
      - Image 实例 → 存 PNG 到 dest_dir/name → 返回 Path
      - grabclipboard() 返回文件路径列表（Win/mac 文件复制）→ 取首个图片扩展名文件，复制进 dest_dir → 返回 Path
      - 无图 / 未装 Pillow / 抓取异常 → None（fail-open）"""
```

- `grab_clipboard_image` 用 counter 名（调用方传 `name`），保持模块 clock-free/可测（同仓库纯模块惯例）。
- Pillow lazy import 在函数内（仿 `file_read._read_pdf` 的 pypdf）；`ImportError`/任意异常 → None。

### 3.3 prompt.py：Ctrl-V 键位 + 占位符插入

- `AgentPrompt.__init__` 加可选注入：`paste_image: Callable[[], Path | None] | None = None`（返回已落盘的绝对/相对 Path 或 None）+ `attachment_relpath: Callable[[Path], str] | None`（把落盘 Path 转 workspace 相对串）。为压依赖，简化成单个回调 `on_paste: Callable[[], str | None]`——返回要插入的相对路径串或 None。prompt.py 只管"调回调、拿到串就插 `[image:串]`"，落盘/相对化逻辑全在 main.py 注入的闭包里（prompt.py 不 import attachments/main，保持 UI 层薄）。
- 键绑定：
  ```python
  @bindings.add("c-v")
  def _paste_image(event):
      if on_paste is None: return
      rel = on_paste()
      if rel:
          event.current_buffer.insert_text(f"[image:{rel}]")
      # ponytail: 无图则静默 no-op（不劫持成文本粘贴；终端 bracketed paste 仍走默认）
  ```
- `on_paste` 为 None（非 tty 回退 / 无注入）时键位无效果，向后兼容。

### 3.4 main.py 接线

- `_build_stdio_read_fn` 增参 `workspace_path` 已有；构造 `attachment_dir = workspace_path/".bareagent_attachments"`（`mkdir(parents=True, exist_ok=True)`，OSError fail-open 回退无粘贴）。持有一个 `counter`（`itertools.count`）。构造 `on_paste` 闭包：调 `grab_clipboard_image(attachment_dir, f"paste-{n}.png")` → 成功返回相对串 `.bareagent_attachments/paste-n.png`，失败 `ui_console.print_status("clipboard: no image found or Pillow not installed")` 返回 None。把 `on_paste` 传进 `AgentPrompt`。
  - 注意：`_build_stdio_read_fn` 目前不持 console。粘贴失败提示可省（返回 None，占位符不插入，用户自见无变化）——或给 `_build_stdio_read_fn` 传 `ui_console`。lazy：传 ui_console 进来打一行提示（低成本、体验好）。
- `/attach <path>` slash 命令（REPL 循环内，登记进 `_SLASH_COMMANDS` + `_HELP_TEXT`）：
  - 纯函数 `_handle_attach_command(arg, *, attachment_dir, workspace_path, counter) -> tuple[str_or_None_rel, str_feedback]`（可单测）：校验 arg 非空 → 源文件存在 + 扩展名在图片白名单（复用 `file_read._IMAGE_EXT_TO_MIME` keys）→ 若已在 workspace 内则直接用其相对路径；否则 `shutil.copy` 进 `attachment_dir/attach-<n>-<basename>` → 返回相对串 + "Attached <name>; it will be sent with your next message." 非图/不存在 → (None, 友好错误)。
  - REPL：命中 `/attach` → 调上函数 → 成功把相对串 append 到会话级 `pending_attachments: list[str]`，`print_status(feedback)`，`continue`。
  - `pending_attachments` 生命周期镜像 `pending_team_messages`：会话切换点（/new /clear /resume /import /fork）`.clear()`。
- 提交拼装（line ~4513 附近，在 `_prepend_pending_context` 之后、`messages.append` 之前）：
  ```python
  text, marker_paths = extract_attachments(text)           # 剥离行内占位符
  all_attach = pending_attachments + marker_paths           # 合并 /attach 与粘贴
  prefix = build_attachment_prefix(_dedup(all_attach))
  if prefix:
      text = prefix + "\n" + text
  pending_attachments.clear()
  ```
  顺序：先 `_prepend_pending_context`（team/workflow）再附件前缀，或反之——附件前缀应贴近本轮用户意图，放最前；team/workflow pending 是异步补投，放其后。二者独立、无强顺序约束，实现时取"附件前缀在最前"。

### 3.5 与 ① 的交互（优雅降级）

若当前模型无 vision：② 仍照常插占位符/前缀，模型调 `read_file` → ① 返回友好 Error → 模型把 Error 转述给用户。可接受的降级（不在 `/attach` 时预判 vision，避免 read/write 两侧耦合 provider）。可在 `/attach` feedback 里附一句"（当前模型可能不支持看图）"——lazy：不做，Error 已足够指引。

### 3.6 optional extra

`pyproject.toml` 加 `[project.optional-dependencies] clipboard = ["Pillow>=10"]`（或复用现有分组命名习惯，仿 `[pdf]`）。未装：`/attach` 全功能可用；Ctrl-V grab 返回 None + 提示。

## 四、契约与兼容性

- `run_read(image_enabled=True)` 默认值保证现有测试/调用字节级不变。
- `get_handlers(image_input_enabled=True)` 默认值同上。
- `Config.capabilities` 尾部 defaulted，现有 `Config(...)` 构造不破。
- provider/loop **零改动**。
- 无 vision 模型且用户从不读图 → 全链路无行为变化。

## 五、测试策略

- `tests/test_capabilities.py`：`supports_image_input` 前缀命中（各家）、未知默认 deny、override True/False 优先级。
- `tests/test_file_read.py`（扩展）：`run_read(image, image_enabled=False)` 返 Error；`=True` 返 blocks（现有图片测试补一条门控）。
- `tests/test_attachments.py`：`extract_attachments`（多占位符、保序去重、无占位符原样）、`build_attachment_prefix`（空/多图）、`grab_clipboard_image`（monkeypatch 假 ImageGrab 返 Image / 路径列表 / None / 抛异常 → None）。
- `tests/test_main.py` 或新 `test_attach_command.py`：`_handle_attach_command`（workspace 内直引 / 外部 copy / 非图错误 / 不存在错误）+ 提交拼装的前缀合并（纯函数层）。
- config 解析：`_parse_capabilities_config` 容错（缺省 None / 非法值 / env 覆盖）。

## 六、刻意简化（ponytail 标注点）

- `.bareagent_attachments/` 单目录不 per-session、不自动清理 — 后续要清理再加 boot 清空。
- 静态前缀表 + 单一全局 `image_in` 覆盖开关，非 per-model dict — 多模型混用需求出现再升级。
- 只做 `image_in` 一个 kind — video/audio 扩展位留注释不建脚手架。
- Ctrl-V 无图静默 no-op — 不实现"回退成文本粘贴"。
