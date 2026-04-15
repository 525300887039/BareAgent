"""六、任务管理验证

适配：TaskManager(task_file=...)，create() 返回 Task 对象，get() 返回 Task 对象，
      list() 返回 list[Task]，get_ready_tasks() 返回 list[Task]。
"""

from src.planning.tasks import TaskManager


def test_task_persistence(tmp_path):
    """任务应持久化到 JSON 文件"""
    store = tmp_path / ".tasks.json"
    mgr = TaskManager(task_file=store)

    task = mgr.create(title="测试任务", description="这是描述")
    assert store.exists()

    # 重新加载
    mgr2 = TaskManager(task_file=store)
    loaded = mgr2.get(task.id)
    assert loaded.title == "测试任务"
    assert loaded.status == "pending"


def test_task_status_transitions(tmp_path):
    """任务状态流转：pending → in_progress → done"""
    store = tmp_path / ".tasks.json"
    mgr = TaskManager(task_file=store)

    task = mgr.create(title="状态测试", description="")
    assert mgr.get(task.id).status == "pending"

    mgr.update(task.id, status="in_progress")
    assert mgr.get(task.id).status == "in_progress"

    mgr.update(task.id, status="done")
    assert mgr.get(task.id).status == "done"


def test_task_list_filter(tmp_path):
    """task_list 应支持按状态过滤"""
    store = tmp_path / ".tasks.json"
    mgr = TaskManager(task_file=store)

    mgr.create(title="任务A", description="")
    task_b = mgr.create(title="任务B", description="")
    mgr.update(task_b.id, status="done")

    pending = mgr.list(status="pending")
    done = mgr.list(status="done")
    assert all(t.status == "pending" for t in pending)
    assert all(t.status == "done" for t in done)


def test_task_dependencies(tmp_path):
    """任务依赖：被依赖任务未完成时不应标记为 ready"""
    store = tmp_path / ".tasks.json"
    mgr = TaskManager(task_file=store)

    t1 = mgr.create(title="前置任务", description="")
    t2 = mgr.create(title="后续任务", description="", depends_on=[t1.id])

    ready = mgr.get_ready_tasks()
    ready_ids = [t.id for t in ready]
    assert t1.id in ready_ids
    assert t2.id not in ready_ids

    mgr.update(t1.id, status="done")
    ready = mgr.get_ready_tasks()
    ready_ids = [t.id for t in ready]
    assert t2.id in ready_ids
