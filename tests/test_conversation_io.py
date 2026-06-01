from __future__ import annotations

import json

import pytest

from src.memory.conversation_io import (
    EXPORT_VERSION,
    parse_import,
    render_markdown,
    to_export_json,
)


def _sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "List the files."},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "I should run ls."},
                {"type": "text", "text": "Running the command."},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "a.txt\nb.txt",
                }
            ],
        },
        {"role": "assistant", "content": "Done."},
    ]


# --- render_markdown ---------------------------------------------------------


def test_render_markdown_includes_user_and_assistant_text():
    md = render_markdown(_sample_messages())
    assert "List the files." in md
    assert "Running the command." in md
    assert "Done." in md


def test_render_markdown_skips_system():
    md = render_markdown(_sample_messages())
    assert "You are a helpful agent." not in md


def test_render_markdown_tool_use_summary_line():
    md = render_markdown(_sample_messages())
    assert "Tool call" in md
    assert "bash" in md
    assert "ls" in md


def test_render_markdown_tool_result_uses_associated_name():
    md = render_markdown(_sample_messages())
    assert "Tool result: bash" in md
    assert "a.txt" in md


def test_render_markdown_thinking_default_omitted():
    md = render_markdown(_sample_messages())
    assert "I should run ls." not in md


def test_render_markdown_thinking_included_when_requested():
    md = render_markdown(_sample_messages(), include_thinking=True)
    assert "I should run ls." in md


def test_render_markdown_truncates_long_tool_result():
    long_output = "x" * 5000
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "bash", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": long_output}],
        },
    ]
    md = render_markdown(messages, max_tool_chars=100)
    assert "… (truncated)" in md
    # full output must not be present
    assert long_output not in md


def test_render_markdown_title_emitted():
    md = render_markdown(_sample_messages(), title="Conversation abc")
    assert md.startswith("# Conversation abc")


def test_render_markdown_empty_does_not_crash():
    md = render_markdown([])
    assert isinstance(md, str)


# --- to_export_json / parse_import round-trip --------------------------------


def test_export_json_wrapper_shape():
    raw = to_export_json(_sample_messages(), session_id="sid", exported_at="2026-01-01T00:00:00")
    payload = json.loads(raw)
    assert payload["version"] == EXPORT_VERSION
    assert payload["session_id"] == "sid"
    assert payload["exported_at"] == "2026-01-01T00:00:00"
    assert payload["messages"] == _sample_messages()


def test_round_trip_preserves_messages_including_system_and_tools():
    messages = _sample_messages()
    raw = to_export_json(messages, session_id="sid", exported_at="2026-01-01T00:00:00")
    restored = parse_import(raw)
    assert restored == messages


# --- parse_import format auto-detection --------------------------------------


def test_parse_import_wrapper_dict():
    messages = _sample_messages()
    raw = to_export_json(messages, session_id="sid", exported_at="ts")
    assert parse_import(raw) == messages


def test_parse_import_bare_list():
    messages = _sample_messages()
    raw = json.dumps(messages, ensure_ascii=False)
    assert parse_import(raw) == messages


def test_parse_import_jsonl():
    messages = _sample_messages()
    jsonl = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages)
    assert parse_import(jsonl) == messages


def test_parse_import_jsonl_skips_blank_lines():
    messages = _sample_messages()
    jsonl = "\n\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n\n"
    assert parse_import(jsonl) == messages


# --- parse_import validation rejection ---------------------------------------


def test_parse_import_rejects_dict_without_messages():
    with pytest.raises(ValueError):
        parse_import(json.dumps({"foo": "bar"}))


def test_parse_import_rejects_plain_string():
    with pytest.raises(ValueError):
        parse_import(json.dumps("just a string"))


def test_parse_import_rejects_element_not_dict():
    with pytest.raises(ValueError):
        parse_import(json.dumps([{"role": "user", "content": "ok"}, "oops"]))


def test_parse_import_rejects_missing_role():
    with pytest.raises(ValueError):
        parse_import(json.dumps([{"content": "no role here"}]))


def test_parse_import_rejects_bad_jsonl_line():
    bad = json.dumps({"role": "user", "content": "ok"}) + "\n{not json}"
    with pytest.raises(ValueError):
        parse_import(bad)
