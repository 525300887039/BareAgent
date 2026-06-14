"""Integration tests: memory tool handler, wiring, isolation, config, injection."""

from __future__ import annotations

from bareagent.core.context import assemble_system_prompt
from bareagent.core.fileutil import atomic_write_text
from bareagent.core.handlers.memory import run_memory
from bareagent.core.tools import MEMORY_TOOL_SCHEMAS, get_handlers, get_tools
from bareagent.main import MemoryConfig, load_config
from bareagent.memory.persistent import MemoryManager
from bareagent.planning.agent_types import resolve_agent_type
from bareagent.planning.subagent import _make_readonly_memory_handler
from tests.conftest import make_test_config

# -- handler dispatch -----------------------------------------------------


def test_run_memory_create_and_view_roundtrip(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    assert "Created" in run_memory(manager=mm, command="create", path="f.md", file_text="hi")
    assert "hi" in run_memory(manager=mm, command="view", path="f.md")


def test_run_memory_missing_required_arg_returns_error(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    out = run_memory(manager=mm, command="create", path="f.md")
    assert out.startswith("Error:")
    assert "file_text" in out


def test_run_memory_unknown_command_returns_error(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    out = run_memory(manager=mm, command="bogus")
    assert out.startswith("Error:")
    assert "unknown memory command" in out


def test_run_memory_traversal_returns_error(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    out = run_memory(manager=mm, command="view", path="../../etc/passwd")
    assert out.startswith("Error:")


def test_run_memory_str_replace_non_unique_returns_error(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    mm.create("f.md", "x x")
    out = run_memory(manager=mm, command="str_replace", path="f.md", old_str="x", new_str="y")
    assert out.startswith("Error:")
    assert "not unique" in out


# -- get_handlers / get_tools wiring -------------------------------------


def test_memory_schema_registered_in_tools():
    names = {tool["name"] for tool in get_tools()}
    assert "memory" in names
    assert len(MEMORY_TOOL_SCHEMAS) == 1


def test_get_handlers_binds_memory_when_manager_present(tmp_path):
    mm = MemoryManager(tmp_path / "m")
    handlers = get_handlers(tmp_path, memory_manager=mm)
    assert "memory" in handlers
    assert "Created" in handlers["memory"](command="create", path="a.md", file_text="x")
    assert "x" in handlers["memory"](command="view", path="a.md")


def test_get_handlers_memory_disabled_without_manager(tmp_path):
    handlers = get_handlers(tmp_path)
    out = handlers["memory"](command="view", path=".")
    assert "disabled" in out.lower()


# -- read-only isolation --------------------------------------------------


def test_readonly_agent_types_cannot_write_memory():
    for name in ("explore", "plan", "code-review"):
        assert resolve_agent_type(name).memory_writable is False
    assert resolve_agent_type("general-purpose").memory_writable is True


def test_readonly_wrapper_allows_view_rejects_writes():
    seen: list[str] = []

    def inner(**kwargs):
        seen.append(kwargs.get("command", ""))
        return "ok"

    wrapped = _make_readonly_memory_handler(inner)

    assert wrapped(command="view", path=".") == "ok"
    for write_cmd in ("create", "str_replace", "insert", "delete", "rename"):
        out = wrapped(command=write_cmd)
        assert "read-only" in out
    # inner only ran for the view command.
    assert seen == ["view"]


# -- system prompt injection ---------------------------------------------


def test_assemble_system_prompt_includes_memory_context(tmp_path):
    prompt = assemble_system_prompt(tmp_path, memory_context="<memory>HOOK</memory>")
    assert "<memory>HOOK</memory>" in prompt


def test_assemble_system_prompt_omits_memory_when_empty(tmp_path):
    prompt = assemble_system_prompt(tmp_path)
    assert "<memory>" not in prompt


# -- config ---------------------------------------------------------------


def test_memory_config_defaults():
    cfg = MemoryConfig()
    assert cfg.enabled is True
    assert cfg.dir == ""
    assert cfg.max_index_lines == 200


def test_config_has_memory_default(tmp_path):
    cfg = make_test_config(tmp_path)
    assert isinstance(cfg.memory, MemoryConfig)
    assert cfg.memory.enabled is True


def test_load_config_parses_memory_section(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n\n'
        '[memory]\nenabled = false\ndir = ".mymem"\nmax_index_lines = 50\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.memory.enabled is False
    assert cfg.memory.dir == ".mymem"
    assert cfg.memory.max_index_lines == 50


def test_load_config_memory_defaults_when_section_absent(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nname = "anthropic"\nmodel = "m"\napi_key_env = "K"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.memory.enabled is True
    assert cfg.memory.max_index_lines == 200


# -- atomic text writer ---------------------------------------------------


def test_atomic_write_text_writes_and_cleans_up(tmp_path):
    target = tmp_path / "sub" / "f.txt"
    atomic_write_text(target, "hello\nworld")
    assert target.read_text(encoding="utf-8") == "hello\nworld"
    leftover = [p for p in (tmp_path / "sub").iterdir() if p.suffix == ".tmp"]
    assert not leftover
