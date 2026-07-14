# 多模态增强：vision 能力门控 + 终端图片输入

## Goal

补齐 BareAgent 的两个多模态短板，两者有前后依赖（① 是 ② 的前置）：

- **① 模型 vision 能力门控**：`read_file` 读图（png/jpg/jpeg/gif/webp → base64 image block）在 Anthropic / OpenAI 两条 provider 都已打通，但没有能力声明——非 vision 模型读图会直接撞 provider API 错误。为 provider/model 声明 `image_in` 能力，`read_file` 图片路径在模型无此能力时返回友好 Error（引导换 vision 模型 / 配置覆盖），而非把 image block 发出去撞 API。
- **② 终端图片输入（粘贴 / attach）**：图片目前只能经工具结果（`read_file`）进对话，用户没法直接"给模型看一张图"。lazy MVP：REPL 粘贴剪贴板图片（或 `/attach <path>`）→ 图片落到 workspace 内会话目录 → 输入框显示 `[image:xxx.png]` 占位符 → 提交时在 user 消息前缀加一句"用户提供了图片 &lt;相对路径&gt;，请用 read_file 查看"，模型走 ① 门控后的 `read_file` 路径。

## Requirements

### ① vision 能力门控

- 为模型声明 `image_in` 能力（模型级事实，非 provider 级——同一 OpenAI provider 既服务 vision 也服务纯文本模型；故不套 `cache_mode` 那种 provider `ClassVar`，改用 presets 式静态表 + 覆盖，对齐 kimi-cli `KIMI_MODEL_CAPABILITIES` 思路）。
- 覆盖优先级：env `BAREAGENT_MODEL_IMAGE_IN`（bool）> config `[capabilities] image_in`（bool，缺省=auto）> 静态已知 vision 模型前缀表。
- **未知模型默认 deny**（fail-safe）：只有确认支持 vision 的模型才放行图片；未列入的模型（含尚未收录的新 vision 模型）返回友好 Error，Error 文案明确指出可用 config/env 覆盖放行。这样彻底杜绝"撞 API"，代价是新 vision 模型需一次性配置或补表。
- `read_file` 是多用途工具（文本/PDF/notebook/图片同一入口），**不做 boot 隐藏**（无法隐藏），改为运行时门控：仅图片扩展名路径受门控，其余路径不变。
- 门控对子代理生效（子代理继承主循环同 provider/model 建的 handlers，天然同门控）。

### ② 终端图片输入

- `/attach <path>`：把用户指定图片（可在 workspace 外、可绝对路径）复制进 workspace 内会话附件目录，登记为待发送附件，回显确认。
- 剪贴板粘贴（Ctrl-V）：抓取剪贴板图片 → 存入同一附件目录 → 在输入框插入 `[image:相对路径]` 占位符，与同行文本一起提交。
- 提交时：把附件（`/attach` 登记的 + 占位符解析出的）转成前缀注入本轮 user 文本（"用户提供了图片 &lt;相对路径&gt;，请用 read_file 查看"），复用现有 `_prepend_pending_context` 同款前缀范式；模型据此调 `read_file` 走已打通路径。
- 附件必须落在 **workspace 内、用相对路径引用**（硬约束：`sandbox.safe_path` 拒绝绝对路径且限定 workspace 内，否则 `read_file` 读不到）。
- 剪贴板依赖（Pillow）为 optional extra（仿 `[pdf]` lazy import）：未装时 `/attach <path>` 仍完全可用，Ctrl-V 粘贴给友好提示、不崩（fail-open）。

## Constraints

- 遵循仓库既有范式：纯逻辑模块可单测（仿 `retry.py` / `goal.py`）、配置逐字段容错不崩 boot、fail-open、新增行为补 pytest。
- **零改 provider / loop**（② lazy MVP 不让 user 消息直接携带 image block——那是完整版扩展位）。
- 源码禁 emoji（仓库 quality-guidelines）。
- 附件目录在 workspace 内需避免污染版本控制（隐藏目录 + 建议 gitignore 提示）。

## Out of Scope（本任务不做，已/另行规划）

- 视频输入、web_fetch 图片、PDF 原生 document block。
- user 消息直接携带 image block（② 完整版）。
- 截图工具。
- 每模型 `image_in` 独立映射表（本任务用"静态前缀表 + 单一全局覆盖开关"，per-model dict 为后续扩展位）。
- video_in / audio_in 等其它能力枚举（本任务只做 `image_in`；能力模块留扩展位但不实现其它 kind）。
- 剪贴板图片的 WxH 尺寸标注、自动缩放/压缩（`read_file` 已有 5 MiB 上限返 Error）。

## Acceptance Criteria

- [ ] 非 vision 模型下 `read_file` 读图返回友好 Error（含覆盖提示），不触发 provider API 图片错误；vision 模型下行为不变（返回 `[text, image]` blocks）。
- [ ] `BAREAGENT_MODEL_IMAGE_IN` / `[capabilities] image_in` 能强制放行或强制拒绝，优先级正确；缺省时走静态前缀表；未知模型默认 deny。
- [ ] 能力模块为纯逻辑、有 pytest 覆盖前缀匹配 + 覆盖优先级 + 未知默认 deny。
- [ ] `/attach <path>`：workspace 外/绝对路径图片被复制进会话附件目录，下一轮提交时前缀注入相对路径；非图片/不存在路径给友好错误。
- [ ] Ctrl-V 粘贴（装了 Pillow 时）：剪贴板图片落盘 + 插入 `[image:相对路径]` 占位符；未装 Pillow 时给友好提示不崩。
- [ ] 提交时占位符/待发送附件被正确解析为前缀，`read_file` 能用该相对路径读到图（结合 ① 门控：vision 模型读到、非 vision 返 Error）。
- [ ] `attachments` 纯逻辑（marker 解析 / 前缀构造 / 剪贴板抓取以 mock 驱动）有 pytest 覆盖。
- [ ] `scripts/ci-check.sh` 四步全绿：ruff check / ruff format --check / pyright / uv run pytest。
