"""Unit tests for the session file-recency tracker (core/file_recency.py)."""

from __future__ import annotations

from bareagent.core.file_recency import FileRecencyTracker


def test_records_most_recent_first():
    t = FileRecencyTracker()
    t.record("a.py")
    t.record("b.py")
    t.record("c.py")
    assert t.recent() == ["c.py", "b.py", "a.py"]


def test_re_record_moves_to_front():
    t = FileRecencyTracker()
    t.record("a.py")
    t.record("b.py")
    t.record("a.py")  # touched again -> most recent
    assert t.recent() == ["a.py", "b.py"]


def test_capacity_evicts_oldest():
    t = FileRecencyTracker(capacity=2)
    t.record("a.py")
    t.record("b.py")
    t.record("c.py")  # evicts a.py
    assert t.recent() == ["c.py", "b.py"]


def test_recent_n_limits():
    t = FileRecencyTracker()
    for name in ("a", "b", "c", "d"):
        t.record(name)
    assert t.recent(2) == ["d", "c"]
    assert t.recent(0) == []


def test_clear_and_empty_record():
    t = FileRecencyTracker()
    t.record("a.py")
    t.record("")  # ignored
    assert t.recent() == ["a.py"]
    t.clear()
    assert t.recent() == []


def test_non_positive_capacity_falls_back_to_default():
    t = FileRecencyTracker(capacity=0)
    for i in range(25):
        t.record(f"f{i}.py")
    # default capacity is 20
    assert len(t.recent()) == 20
