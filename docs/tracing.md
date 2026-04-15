# BareAgent Tracing 系统

## 为什么重构

### 背景

BareAgent 最初使用自建的 `InteractionLogger`（`src/debug/interaction_log.py`）作为唯一的可观测性手段。它将每次 LLM 请求/响应以 JSON 文件写入 `.logs/` 目录，并提供了 Web Viewer 和 `/log` 命令来查看。

虽然功能完整，但存在以下问题：

1. **无法对接外部平台** — 不能接入 Langfuse、OpenTelemetry 等行业标准的 LLM 可观测性平台
2. **不符合行业规范** — LangChain、Haystack、OpenAI Agents SDK 等主流框架均采用 **抽象 Tracer 接口 + 可插拔后端** 的模式
3. **子智能体盲区** — `subagent.py` 和 `autonomous.py` 中的 `agent_loop()` 调用不传 `interaction_logger`，子智能体/团队智能体的 LLM 调用完全不可见
4. **工具执行不可见** — 只追踪 LLM 调用级别，工具执行（handler 调用）没有独立的追踪

### 行业趋势

| 框架 | 模式 |
|---|---|
| **Haystack** | `Tracer` ABC + `ProxyTracer` 全局单例 + 运行时热替换 |
| **OpenAI Agents SDK** | `TracingProcessor` ABC + `BatchTraceProcessor` + `Exporter` |
| **LlamaIndex / Haystack / OpenAI** | 通过 `openinference-instrumentation-*` 走 OpenTelemetry |
| **LangChain** | `BaseCallbackHandler` 回调模式（较老，事件词表庞大） |

我们选择了 **Haystack 的 Tracer + ProxyTracer 模式**，因为它是最简洁的抽象，且天然支持 OpenTelemetry 集成。

### 设计原则

- **InteractionLogger 不修改** — 新系统通过组合模式包装它
- **零开销默认** — 未配置任何后端时使用 `NullTracer`，无任何性能损失
- **两套系统并存** — `interaction_logger` 继续负责 JSON 文件持久化（`/log` 和 web_viewer），`tracer` 负责标准化 span 上报
- **可选依赖** — `langfuse` 和 `opentelemetry-*` 不是硬依赖

---

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                      应用层                                  │
│  agent_loop  /  subagent  /  autonomous_agent               │
│                                                             │
│  from src.tracing import tracer                             │
│  with tracer.trace("llm_call", tags={...}) as span:         │
│      response = provider.create(...)                        │
│      span.set_tag("input_tokens", response.input_tokens)    │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│                ProxyTracer (全局单例)                        │
│  默认: NullTracer (零开销)                                   │
│  运行时热替换: enable_tracing(my_tracer)                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼──────┐
│ JsonFile   │  │ Langfuse   │  │ OpenTelemetry│
│ Tracer     │  │ Tracer     │  │ Tracer       │
│            │  │            │  │              │
│ (包装      │  │ (langfuse  │  │ (OTel SDK)   │
│ Interaction│  │  SDK)      │  │              │
│ Logger)    │  │            │  │              │
└──────┬─────┘  └────────────┘  └──────────────┘
       │
┌──────▼─────────────────┐
│ InteractionLogger      │
│ (不修改，原样保留)      │
│ .logs/{session}/{seq}  │
│ Web Viewer / /log      │
└────────────────────────┘
```

当多个后端同时启用时，通过 `CompositeTracer` 扇出：

```
ProxyTracer → CompositeTracer → [JsonFileTracer, LangfuseTracer]
```

---

## 快速上手

### 零配置 (默认)

不需要任何配置。`NullTracer` 自动生效，零开销。

```bash
bareagent
```

旧的 JSON 文件日志通过 `[debug] enabled = true` 独立控制，和新 tracing 系统互不影响。

### 启用 Langfuse

```bash
# 方式一：环境变量（推荐，自动检测）
export LANGFUSE_PUBLIC_KEY=pk-lf-xxx
export LANGFUSE_SECRET_KEY=sk-lf-xxx
export LANGFUSE_HOST=https://cloud.langfuse.com   # 可选，默认 cloud

pip install bareagent[langfuse]
bareagent
```

```toml
# 方式二：config.toml 显式启用
[tracing]
langfuse = true
```

### 启用 OpenTelemetry

```bash
# 方式一：环境变量（自动检测）
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

pip install bareagent[otel]
bareagent
```

```toml
# 方式二：config.toml 显式启用
[tracing]
opentelemetry = true
```

### 同时启用多个后端

```toml
[debug]
enabled = true   # JSON 文件日志

[tracing]
langfuse = true  # 同时发到 Langfuse
```

---

## 配置参考

### `config.toml` 中的 `[tracing]` 节

```toml
[tracing]
# langfuse = false
#   启用 Langfuse 后端。也可通过设置 LANGFUSE_PUBLIC_KEY 环境变量自动启用。
#   需要安装: pip install bareagent[langfuse]

# opentelemetry = false
#   启用 OpenTelemetry 后端。也可通过设置 OTEL_EXPORTER_OTLP_ENDPOINT 自动启用。
#   需要安装: pip install bareagent[otel]

# content_enabled = true
#   是否在 traces 中包含消息内容（PII 敏感场景可设为 false）。
#   也可通过 BAREAGENT_CONTENT_TRACING_ENABLED 环境变量控制。
```

### 环境变量

| 变量 | 用途 |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | Langfuse 公钥（设置即自动启用 Langfuse） |
| `LANGFUSE_SECRET_KEY` | Langfuse 私钥 |
| `LANGFUSE_HOST` | Langfuse 服务器地址（默认 cloud） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel OTLP endpoint（设置即自动启用 OTel） |
| `BAREAGENT_CONTENT_TRACING_ENABLED` | `true`/`false`，控制是否上报消息内容 |
| `BAREAGENT_TRACING_LANGFUSE` | `true`/`false`，显式启用/禁用 Langfuse |
| `BAREAGENT_TRACING_OPENTELEMETRY` | `true`/`false`，显式启用/禁用 OTel |

### 新旧系统切换

| `[debug] enabled` | `[tracing]` | 效果 |
|---|---|---|
| `true` | 全 `false` | 只用旧方式（JSON 文件，和重构前完全一致） |
| `false` | `langfuse=true` | 只用新方式（只发 Langfuse，无本地文件） |
| `true` | `langfuse=true` | **两种同时开**（JSON 文件 + Langfuse） |
| `false` | 全 `false` | 都不开（NullTracer，零开销） |

---

## 开发者指南：实现自定义 Tracer 后端

只需实现两个 ABC：

```python
from src.tracing._api import Span, Tracer

class MySpan(Span):
    def set_tag(self, key, value):
        # 写入你的后端
        ...

    def set_content_tag(self, key, value):
        # 内容敏感数据，可选择性跳过
        ...

    def set_error(self, error):
        ...

    def end(self):
        ...

class MyTracer(Tracer):
    @contextlib.contextmanager
    def trace(self, operation_name, tags=None, *, parent_span=None):
        span = MySpan(...)
        yield span
        span.end()

    def current_span(self):
        return self._current

    def flush(self):
        ...

    def shutdown(self):
        ...
```

注册到全局：

```python
from src.tracing import enable_tracing
enable_tracing(MyTracer())
```

---

## Span 层次结构

```
session (Langfuse trace)
├── llm_call (tags: model, input_tokens, output_tokens)
├── tool_execution (tags: tool)
│   └── (handler 执行)
├── llm_call
├── tool_execution
│   └── subagent (tags: agent_type, depth)
│       ├── llm_call
│       ├── tool_execution
│       └── llm_call
├── llm_call (最终回复)
└── teammate_run (tags: agent)
    ├── llm_call
    └── tool_execution
```

---

## 与 InteractionLogger 的关系

`InteractionLogger` 并未被替换或修改。新的 tracing 系统通过以下方式与其共存：

1. `JsonFileTracer` 通过 **组合模式** 包装 `InteractionLogger`
2. `agent_loop` 中 **两套系统并行运行**：
   - `interaction_logger` 参数 → `_safe_log_request` / `_safe_log_response` → JSON 文件
   - `global_tracer.trace()` → 标准化 span → 任意后端
3. `/log` 命令和 Web Viewer 继续直接使用 `InteractionLogger`

这样做的好处：
- 零风险 — 旧系统完全不变
- 渐进迁移 — 可以先用新 tracing 验证，再决定是否简化旧系统
- 向后兼容 — 所有现有测试通过

---

## 文件清单

### 新建文件

| 文件 | 用途 |
|---|---|
| `src/tracing/__init__.py` | 公共 API 导出 |
| `src/tracing/_api.py` | `Span` / `Tracer` ABC + `NullSpan` / `NullTracer` |
| `src/tracing/_proxy.py` | `ProxyTracer` 全局单例 + `enable_tracing()` |
| `src/tracing/json_file.py` | `JsonFileTracer`，组合包装 `InteractionLogger` |
| `src/tracing/composite.py` | `CompositeTracer`，扇出到 N 个后端 |
| `src/tracing/langfuse.py` | `LangfuseTracer`，Langfuse SDK 后端 |
| `src/tracing/otel.py` | `OpenTelemetryTracer`，OTel SDK 后端 |
| `src/tracing/setup.py` | `configure_tracing()` 配置驱动初始化 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `src/core/loop.py` | 新增 `llm_call` 和 `tool_execution` span 埋点 |
| `src/planning/subagent.py` | 新增 `subagent` span 包裹 |
| `src/team/autonomous.py` | 新增 `teammate_run` span 包裹 |
| `src/main.py` | 新增 `TracingConfig`，调用 `configure_tracing()` |
| `src/ui/app.py` | 同步 tracing 初始化 |
| `config.toml` | 新增 `[tracing]` 配置节 |
| `pyproject.toml` | 新增 `langfuse` 和 `otel` 可选依赖组 |

### 未修改文件

| 文件 | 原因 |
|---|---|
| `src/debug/interaction_log.py` | 保持原样，通过组合模式被包装 |
| `src/debug/web_viewer.py` | 继续直接使用 `InteractionLogger` |
