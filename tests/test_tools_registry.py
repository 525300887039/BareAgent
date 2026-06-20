"""四、工具系统验证 — 4.1 工具注册完整性

适配：get_tools() 返回 schema list，DEFERRED_TOOLS 是 set。
"""

from bareagent.core.tools import BASE_TOOLS, DEFERRED_TOOLS, get_tools


def test_base_tools_registered():
    """6 个基础工具必须注册"""
    schemas = get_tools()
    names = {s["name"] for s in schemas}
    assert BASE_TOOLS.issubset(names), f"Missing tools: {BASE_TOOLS - names}"


def test_base_tools_have_parameters():
    """所有基础工具 schema 必须包含 parameters"""
    schemas = get_tools()
    for s in schemas:
        if s["name"] in BASE_TOOLS:
            assert "parameters" in s, f"Tool {s['name']} missing parameters"


def test_deferred_tools_exist():
    """延迟加载工具应包含 todo/task/subagent 等"""
    expected_names = {
        "todo_read",
        "todo_write",
        "task_create",
        "task_list",
        "subagent",
        "load_skill",
    }
    assert expected_names.issubset(DEFERRED_TOOLS), (
        f"Missing deferred tools: {expected_names - DEFERRED_TOOLS}"
    )


# Boot-gated DEFERRED tools: present in DEFERRED_TOOLS but only injected into
# get_tools() when their backing dependency is wired (e.g. code_search needs a
# usable embedder / CodeIndex; repo_map needs the tree-sitter [repo-map] extra),
# mirroring how MCP/LSP tools only appear when configured. They are excluded from
# the "always in schema" invariant below.
_BOOT_GATED_DEFERRED_TOOLS = {"code_search", "repo_map"}


def test_all_tools_in_schema():
    """所有 BASE_TOOLS 和（非 boot-gated 的）DEFERRED_TOOLS 都应出现在 get_tools() schema 中"""
    schemas = get_tools()
    names = {s["name"] for s in schemas}
    all_expected = (BASE_TOOLS | DEFERRED_TOOLS) - _BOOT_GATED_DEFERRED_TOOLS
    assert all_expected.issubset(names), f"Missing from schema: {all_expected - names}"
    # Boot-gated tools stay hidden until their dependency is wired.
    assert _BOOT_GATED_DEFERRED_TOOLS.isdisjoint(names)
