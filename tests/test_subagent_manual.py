"""十一、子智能体验证

适配：run_subagent(provider, task, tools, handlers, permission, ..., current_depth=, max_depth=)
深度超限时返回字符串而非抛异常。
"""
from src.planning.subagent import run_subagent


def test_subagent_depth_limit():
    """子智能体递归深度超限应返回拒绝消息"""
    result = run_subagent(
        provider=None,
        task="test",
        tools=[],
        handlers={},
        permission=None,
        current_depth=4,
        max_depth=3,
    )
    assert "depth" in result.lower()
    assert "exceeds" in result.lower() or "refused" in result.lower()
