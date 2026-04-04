"""八、消息压缩验证

适配：_micro_compact(msgs, keep_recent=3) 就地修改，带下划线前缀。
"""
from src.memory.compact import _micro_compact


def _make_messages(n):
    """生成 n 条模拟消息"""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"Question {i}"})
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "bash",
                 "input": {"command": f"echo {i}"}},
            ]
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": f"Output line {'x' * 500} end"}
            ]
        })
    return msgs


def test_micro_compact_truncates_old():
    """微压缩应截断旧的工具结果，保留最近 3 条"""
    msgs = _make_messages(10)
    _micro_compact(msgs, keep_recent=3)
    truncated_count = sum(
        1 for m in msgs
        if isinstance(m.get("content"), list)
        and any("truncated" in str(c).lower() for c in m["content"]
                if isinstance(c, dict))
    )
    assert truncated_count > 0, "Should have truncated some old results"


def test_micro_compact_preserves_system():
    """微压缩应保留 system 消息"""
    msgs = _make_messages(5)
    _micro_compact(msgs, keep_recent=3)
    assert msgs[0]["role"] == "system"
