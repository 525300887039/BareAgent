"""四、工具系统验证 — 4.3 搜索工具测试

适配：run_glob(pattern, workspace=...) 返回 list[str]，
      run_grep(pattern, path=..., workspace=...) 返回 list[str]
"""

from pathlib import Path

from bareagent.core.handlers.glob_search import run_glob
from bareagent.core.handlers.grep_search import run_grep

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
    result = run_grep("def agent_loop", path="src/bareagent/core/loop.py", workspace=WORKSPACE)
    assert isinstance(result, list)
    assert any("agent_loop" in line for line in result)


def test_grep_no_match():
    """grep 无匹配应返回空列表"""
    result = run_grep("THIS_STRING_DOES_NOT_EXIST_ANYWHERE_XYZ", path="src/", workspace=WORKSPACE)
    assert result == []


# --------------------------------------------------------------------------- #
# grep output_mode (content | files_with_matches | count)
# --------------------------------------------------------------------------- #
def _make_grep_workspace(tmp_path: Path) -> Path:
    # iter_search_files walks files in sorted name order, so output is
    # deterministic: a.txt before b.txt, line order within a file.
    (tmp_path / "a.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("alpha\ngamma\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("nothing here\n", encoding="utf-8")
    return tmp_path


def test_grep_content_mode_is_default(tmp_path):
    """默认 content 档返回 file:line:text，显式 content 与默认一致"""
    ws = _make_grep_workspace(tmp_path)
    expected = ["a.txt:1:alpha", "a.txt:3:alpha", "b.txt:1:alpha"]
    assert run_grep("alpha", workspace=ws) == expected
    assert run_grep("alpha", output_mode="content", workspace=ws) == expected


def test_grep_files_with_matches_dedupes_files(tmp_path):
    """files_with_matches 只回匹配文件且去重（a.txt 两处匹配只出现一次）"""
    ws = _make_grep_workspace(tmp_path)
    assert run_grep("alpha", output_mode="files_with_matches", workspace=ws) == [
        "a.txt",
        "b.txt",
    ]


def test_grep_count_mode_reports_per_file_counts(tmp_path):
    """count 档回 file:count"""
    ws = _make_grep_workspace(tmp_path)
    assert run_grep("alpha", output_mode="count", workspace=ws) == ["a.txt:2", "b.txt:1"]


def test_grep_invalid_output_mode_falls_back_to_content(tmp_path):
    """非法 output_mode 优雅退化为 content"""
    ws = _make_grep_workspace(tmp_path)
    assert run_grep("alpha", output_mode="bogus", workspace=ws) == [
        "a.txt:1:alpha",
        "a.txt:3:alpha",
        "b.txt:1:alpha",
    ]


def test_grep_no_match_returns_empty_in_all_modes(tmp_path):
    """三档在无匹配时都返回空列表"""
    ws = _make_grep_workspace(tmp_path)
    for mode in ("content", "files_with_matches", "count"):
        assert run_grep("ZZZ_NOPE", output_mode=mode, workspace=ws) == []
