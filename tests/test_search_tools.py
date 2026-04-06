"""四、工具系统验证 — 4.3 搜索工具测试

适配：run_glob(pattern, workspace=...) 返回 list[str]，
      run_grep(pattern, path=..., workspace=...) 返回 list[str]
"""
from pathlib import Path

from src.core.handlers.glob_search import run_glob
from src.core.handlers.grep_search import run_grep

WORKSPACE = Path(__file__).resolve().parents[1]


def test_glob_py_files():
    """glob *.py 应找到 Python 文件"""
    result = run_glob("src/**/*.py", workspace=WORKSPACE)
    assert isinstance(result, list)
    assert any("loop.py" in f for f in result) or any("main.py" in f for f in result)


def test_glob_no_match():
    """glob 无匹配应返回空列表"""
    result = run_glob("*.nonexistent_extension_xyz", workspace=WORKSPACE)
    assert result == []


def test_grep_find_function():
    """grep 应找到函数定义"""
    result = run_grep("def agent_loop", path="src/core/loop.py", workspace=WORKSPACE)
    assert isinstance(result, list)
    assert any("agent_loop" in line for line in result)


def test_grep_no_match():
    """grep 无匹配应返回空列表"""
    result = run_grep("THIS_STRING_DOES_NOT_EXIST_ANYWHERE_XYZ",
                      path="src/", workspace=WORKSPACE)
    assert result == []
