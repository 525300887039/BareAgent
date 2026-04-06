"""四、工具系统验证 — 4.2 文件工具功能测试

适配：run_read(file_path, workspace=...), run_write(file_path, content, workspace=...),
      run_edit(file_path, old_text, new_text, workspace=...)
"""
from src.core.handlers.file_read import run_read
from src.core.handlers.file_write import run_write
from src.core.handlers.file_edit import run_edit


def test_write_then_read(tmp_path):
    """写入文件后应能正确读取"""
    run_write(file_path="test.txt", content="hello world", workspace=tmp_path)
    result = run_read(file_path="test.txt", workspace=tmp_path)
    assert "hello world" in result


def test_edit_file_replace(tmp_path):
    """edit_file 应正确替换字符串"""
    p = tmp_path / "test.txt"
    p.write_text("foo bar baz")
    run_edit(file_path="test.txt", old_text="bar", new_text="qux", workspace=tmp_path)
    assert p.read_text() == "foo qux baz"


def test_edit_file_not_found(tmp_path):
    """编辑不存在的字符串应报错"""
    p = tmp_path / "test.txt"
    p.write_text("hello")
    try:
        run_edit(file_path="test.txt", old_text="nonexistent",
                 new_text="replacement", workspace=tmp_path)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not found" in str(e).lower()


def test_read_file_not_found(tmp_path):
    """读取不存在的文件应报错"""
    try:
        run_read(file_path="nonexistent.txt", workspace=tmp_path)
        assert False, "Should have raised"
    except (FileNotFoundError, OSError, ValueError):
        pass


def test_write_file_utf8(tmp_path):
    """写入和读取中文内容"""
    run_write(file_path="chinese.txt", content="你好世界\n测试中文", workspace=tmp_path)
    result = run_read(file_path="chinese.txt", workspace=tmp_path)
    assert "你好世界" in result
    assert "测试中文" in result
