# LLM 提供商

## 抽象基类 BaseLLMProvider

<!-- provider/base.py：定义 create() 和 create_stream() 接口 -->

## AnthropicProvider

<!-- provider/anthropic.py：Anthropic API 集成，支持扩展思考 -->

## OpenAIProvider

<!-- provider/openai.py：OpenAI 兼容 API 集成 -->

## 工厂模式

<!-- provider/factory.py：根据配置创建对应提供商实例 -->

## LLMResponse 统一响应结构

<!-- 包含：text, tool_calls, thinking, usage（token 计数） -->

## 流式 vs 非流式

<!-- create_stream() 返回异步迭代器；create() 返回完整响应 -->

## 扩展思考

<!-- ThinkingConfig：mode（adaptive/always/off）、budget_tokens -->
