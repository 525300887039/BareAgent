# web_search 默认改用 Bing HTML 抓取（免 key 免费）

## Goal

让 BareAgent 的 `web_search` 在**不配置任何 API key** 的情况下开箱可用。当前默认后端抓 DuckDuckGo，已被 DDG 的反爬（HTTP 202 challenge/anomaly 页）全面拦截，导致所有查询静默返回 "No results found"。改为抓 **Bing HTML 结果页**作为免 key 默认后端（Bing 对抓取宽松、国内可直连），并保留 Brave / SearXNG 作为可选增强后端。

## What I already know（已实测/已查证）

- `run_web_search`（`src/core/handlers/web_search.py`）当前逻辑：有 `BRAVE_SEARCH_API_KEY` → Brave API；否则 → `_search_ddg_html` 抓 DDG。
- handler 是**纯 env-var 驱动**，不接收 config 对象（与 Brave 一致）；注册见 `src/core/tools.py`（`BASE_TOOLS` + `_HANDLERS`/`build_handlers`），schema 在 `tools.py:319`。
- 测试 `tests/test_web_search.py` 用 `unittest.mock.patch` mock `_search_ddg_html` / `_search_brave`，不打真实网络。
- **DDG 实测**：四条链路（GET/POST、BareAgent/浏览器 UA、html/lite 端点）全部 HTTP 202 + challenge/anomaly 页，`result__a` 0 命中 → DDG 抓取已死。
- **Bing 实测（关键发现）**：`www.bing.com/search` 和 `cn.bing.com/search` 对 **现代 Chrome UA 只返回 JS 外壳**（`class="b_algo"` 0 命中，结果靠前端 JS 注入）；换 **轻量/非 JS UA（如 Lynx `Lynx/2.8.9rel.1 libwww-FM/2.14`）→ 服务端直接渲染 `<li class="b_algo">`，10 命中**。这就是 DeepSeek-Reasonix 默认 Bing 后端免 key 的原理。
- Reasonix 佐证：官方文档 "Default backend is **Bing** (works from CN without proxy)"，无需独立 search key；其余后端（Tavily/Perplexity/Exa/Brave/Bing-API）才要 key，免 key 路径只有 Bing 抓取与自建 SearXNG。

## Requirements（evolving）

- R1：新增 `_search_bing_html(query, max_results, timeout)`，用非 JS UA 抓 `www.bing.com/search`，解析 `<li class="b_algo">` 的标题/URL/snippet，返回与现有后端一致的 `list[dict[str,str]]`（title/url/snippet）。
- R2：`run_web_search` 后端选择改为：`Brave（有 key）→ SearXNG（有 base_url，若纳入）→ Bing HTML（默认）→ [可选] DDG（保底）`，全部 env-var 自动探测。
- R3：挑战页/反爬显式报错——某后端返回 0 结果且响应体含反爬标记（202 / challenge / anomaly / captcha）时，返回明确的 `Error: ...` 字符串而非静默 "No results found"，避免误导 LLM 空转重试。
- R4：补 pytest（mock 网络，验证后端选择优先级 + Bing HTML 解析 + 挑战页报错），不打真实网络（沿用现有 mock 风格）。

## Acceptance Criteria（evolving）

- [ ] 无任何 key、无 SearXNG 时，`run_web_search("github trending")` 走 Bing HTML 返回非空结构化结果。
- [ ] Bing HTML 解析能从真实结果页样本提取 title/url/snippet（用固定 HTML fixture 测，不打网）。
- [ ] 后端选择优先级有单测覆盖（Brave key 存在走 Brave；否则走 Bing）。
- [ ] 反爬/挑战页触发显式 `Error:` 报错路径有单测覆盖。
- [ ] `ruff check` 通过；新增/改动文件已格式化（仅改动文件，勿全树 format）。

## Definition of Done

- 新增行为有 pytest 覆盖，默认 `pytest`（排除 manual）全绿。
- `ruff check src tests` 通过。
- CLAUDE.md 工具系统段落若涉及 web_search 后端描述需同步（行为变化）。

## Out of Scope（explicit）

- 不引入 config.toml `[web_search]` 穿透（保持 env-var 驱动，与 Brave 一致）。
- 不实现浏览器/JS 渲染抓取、不引入第三方爬虫依赖（纯 stdlib + 正则，与现有风格一致）。
- 代理轮换 / Tor 等绕限流手段不做。
- **SearXNG 后端本次不实现**（留待后续任务）；本次只保证设计上可加（后端选择是 env-var 自动探测，加一支即可）。
- **移除 DDG 抓取**（`_search_ddg_html` 及其测试一并删除）——已被 202 挡死，留着是 dead code。

## Decision (ADR-lite)

- **Context**：默认后端 DDG 抓取被反爬 202 挡死，需要一个免 key、国内可直连的替代。
- **Decision**：(1) 新增 Bing HTML 抓取（非 JS UA + `b_algo` 解析）作为免 key 默认后端；(2) 后端链路 `Brave(有 key) → Bing(默认)`，env-var 自动探测；(3) 删除 DDG 抓取；(4) SearXNG 延后；(5) 反爬/空结果显式报错。
- **Consequences**：免 key 开箱可用且国内直连；依赖 Bing 对非 JS UA 返回服务端渲染结果这一行为（若 Bing 改版需调整 UA/解析）；SearXNG 用户需等后续任务。

## Technical Notes

- Bing 解析：`<li class="b_algo">` 内 `<h2><a href="URL">TITLE</a></h2>`，snippet 常在 `<p>` 或 `.b_caption p` / `.b_lineclamp*`。实现期需对真实样本写正则（可现抓一页存为 test fixture）。
- 非 JS UA 实测可用：`Lynx/2.8.9rel.1 libwww-FM/2.14`（10 个 b_algo）；现代 Chrome UA 不可用（0 个）。
- 复用 `web_fetch.html_to_text` 清洗 snippet HTML（与 `_search_ddg_html` 一致）。
- 关键文件：`src/core/handlers/web_search.py`、`tests/test_web_search.py`、必要时 `CLAUDE.md`。
</content>
