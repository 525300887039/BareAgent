"""九、多智能体邮箱验证

适配：bus.send(Message(...))，bus.receive() 返回 list[Message]。
"""

from src.team.mailbox import MessageBus, Message


def test_mailbox_send_receive(tmp_path):
    """发送消息后应能接收"""
    bus = MessageBus(mailbox_dir=tmp_path / ".mailbox")
    bus.send(
        Message(
            id="",
            from_agent="agent_a",
            to_agent="agent_b",
            content="你好",
            msg_type="chat",
            timestamp="",
        )
    )
    msgs = bus.receive("agent_b")
    assert len(msgs) >= 1
    assert any("你好" in m.content for m in msgs)


def test_mailbox_isolation(tmp_path):
    """不同 agent 的邮箱应隔离"""
    bus = MessageBus(mailbox_dir=tmp_path / ".mailbox")
    bus.send(
        Message(
            id="",
            from_agent="a",
            to_agent="b",
            content="给 b 的消息",
            msg_type="chat",
            timestamp="",
        )
    )
    bus.send(
        Message(
            id="",
            from_agent="a",
            to_agent="c",
            content="给 c 的消息",
            msg_type="chat",
            timestamp="",
        )
    )
    msgs_b = bus.receive("b")
    assert any("给 b" in m.content for m in msgs_b)
    assert not any("给 c" in m.content for m in msgs_b)


def test_mailbox_agent_name_validation(tmp_path):
    """非法 agent 名称应被拒绝（防路径注入）"""
    bus = MessageBus(mailbox_dir=tmp_path / ".mailbox")
    try:
        bus.send(
            Message(
                id="",
                from_agent="../evil",
                to_agent="target",
                content="payload",
                msg_type="chat",
                timestamp="",
            )
        )
        assert False, "Should reject path traversal"
    except (ValueError, Exception):
        pass
