"""五、TODO 管理验证

适配：mgr.add() 返回格式化字符串（非 id），mgr.list() 返回格式化字符串。
改用 mgr.tasks 字典直接验证。
"""
from src.planning.todo import TodoManager


def test_todo_lifecycle():
    """TODO 完整生命周期：添加 → 更新 → 读取 → 完成"""
    mgr = TodoManager()

    # 添加
    result = mgr.add("修复登录 bug", priority="high")
    assert "t1" in result
    assert len(mgr.tasks) == 1

    # 读取
    listing = mgr.list()
    assert "修复登录 bug" in listing

    # 更新状态
    mgr.update("t1", status="in_progress")
    assert mgr.tasks["t1"]["status"] == "in_progress"

    # 完成
    mgr.update("t1", status="done")
    assert mgr.tasks["t1"]["status"] == "done"


def test_todo_priority():
    """TODO 优先级应正确存储"""
    mgr = TodoManager()
    mgr.add("低优先级", priority="low")
    mgr.add("高优先级", priority="high")
    assert mgr.tasks["t1"]["priority"] == "low"
    assert mgr.tasks["t2"]["priority"] == "high"
