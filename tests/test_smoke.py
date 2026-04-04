"""第十二节：端到端冒烟测试（需要真实 API Key）。

使用 config.local.toml 中配置的 OpenAI 兼容 API 进行真实调用。
无 OPENAI_API_KEY 环境变量时自动跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.loop import agent_loop
from src.core.tools import get_handlers, get_tools
from src.main import load_config, resolve_config_path
from src.permission.guard import PermissionGuard, PermissionMode
from src.provider.factory import create_provider

SKIP = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set, skipping smoke tests",
)

CONFIG_PATH = resolve_config_path(None)


def _make_provider():
    config = load_config(CONFIG_PATH)
    return create_provider(config)


def _make_handlers(workspace: Path, provider=None):
    tools = get_tools()
    permission = PermissionGuard(PermissionMode.BYPASS)
    return get_handlers(
        workspace=workspace,
        provider=provider,
        tools=tools,
        permission=permission,
    ), tools, permission


# ── 12.1 端到端对话测试 ──────────────────────────────────────────────


@SKIP
def test_e2e_conversation(tmp_path: Path):
    """LLM 能正常返回文本回复，无异常。"""
    provider = _make_provider()
    handlers, tools, permission = _make_handlers(tmp_path, provider)
    messages: list[dict] = [{"role": "user", "content": "请用一句话介绍你自己"}]

    result = agent_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        handlers=handlers,
        permission=permission,
        stream=False,
        max_iterations=10,
    )

    assert isinstance(result, str)
    assert len(result.strip()) > 0, "LLM 应返回非空文本"


# ── 12.2 工具调用端到端（bash） ──────────────────────────────────────


@SKIP
def test_e2e_bash_tool(tmp_path: Path):
    """LLM 调用 bash 工具执行 echo 命令，结果出现在历史中。"""
    provider = _make_provider()
    handlers, tools, permission = _make_handlers(tmp_path, provider)
    messages: list[dict] = [
        {"role": "user", "content": '请执行 bash 命令 echo "hello from bareagent"'}
    ]

    result = agent_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        handlers=handlers,
        permission=permission,
        stream=False,
        max_iterations=10,
    )

    # 检查返回文本或 messages 历史中包含预期输出
    full_text = result + " ".join(
        str(m.get("content", "")) for m in messages if isinstance(m, dict)
    )
    assert "hello from bareagent" in full_text, (
        f"预期 'hello from bareagent' 出现在输出中，实际: {full_text[:500]}"
    )


# ── 12.3 文件操作端到端 ──────────────────────────────────────────────


@SKIP
def test_e2e_file_operations(tmp_path: Path):
    """LLM 创建并读取文件，验证文件实际存在且内容正确。"""
    provider = _make_provider()
    handlers, tools, permission = _make_handlers(tmp_path, provider)
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                '请创建文件 smoke_test.txt 内容为 "test ok"，然后读取它'
            ),
        }
    ]

    agent_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        handlers=handlers,
        permission=permission,
        stream=False,
        max_iterations=10,
    )

    target = tmp_path / "smoke_test.txt"
    assert target.exists(), f"文件 {target} 应被创建"
    content = target.read_text(encoding="utf-8")
    assert "test ok" in content, f"文件内容应包含 'test ok'，实际: {content!r}"
