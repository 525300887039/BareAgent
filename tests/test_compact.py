from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from src.memory.compact import _micro_compact, Compactor
from src.memory.token_counter import estimate_tokens
from src.memory.transcript import TranscriptManager
from src.provider.base import BaseLLMProvider, LLMResponse


class StubProvider(BaseLLMProvider):
    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, messages, tools, **kwargs) -> LLMResponse:
        self.calls.append(
            {
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
                "kwargs": deepcopy(kwargs),
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    def create_stream(self, messages, tools, **kwargs):
        _ = messages, tools, kwargs
        raise NotImplementedError


def test_estimate_tokens_counts_mixed_chinese_english_and_tool_result() -> None:
    messages = [
        {"role": "user", "content": "你好abc123"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_result", "content": "结果42"},
            ],
        },
    ]

    assert estimate_tokens(messages) == 9


def test_micro_compact_truncates_old_tool_results_in_place() -> None:
    messages = [
        {"role": "system", "content": "You are BareAgent."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "first result"}
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "second result"}
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_3", "name": "grep", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_3", "content": "third result"}
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_4", "name": "glob", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_4", "content": "fourth result"}
            ],
        },
    ]

    _micro_compact(messages, keep_recent=3)

    assert messages[2]["content"][0]["content"] == "[truncated: bash result, 12 chars]"
    assert messages[4]["content"][0]["content"] == "second result"
    assert messages[6]["content"][0]["content"] == "third result"
    assert messages[8]["content"][0]["content"] == "fourth result"


def test_auto_compact_summarizes_when_threshold_is_exceeded(tmp_path: Path) -> None:
    provider = StubProvider(
        response=LLMResponse(
            text="总结后的上下文",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
        )
    )
    transcript_mgr = TranscriptManager(tmp_path / ".transcripts")
    compact = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        threshold=3,
        session_id="session-alpha",
    )
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "之前的需求"},
        {"role": "assistant", "content": "之前已经完成一部分"},
        {"role": "user", "content": "请继续处理最新请求"},
    ]

    compact(messages)

    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0]["role"] == "system"
    assert "上下文压缩助手" in provider.calls[0]["messages"][0]["content"]
    summary_prompt = provider.calls[0]["messages"][1]["content"]
    assert "之前的需求" in summary_prompt
    assert "之前已经完成一部分" in summary_prompt
    assert "请继续处理最新请求" not in summary_prompt
    assert messages == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "[Context Compressed]\n总结后的上下文"},
        {"role": "assistant", "content": "收到，我已理解之前的上下文，继续工作。"},
        {"role": "user", "content": "请继续处理最新请求"},
    ]
    saved_files = list((tmp_path / ".transcripts").glob("session-alpha_*.jsonl"))
    assert len(saved_files) == 1


def test_auto_compact_keeps_messages_when_summary_fails(tmp_path: Path) -> None:
    provider = StubProvider(error=RuntimeError("summary failed"))
    transcript_mgr = TranscriptManager(tmp_path / ".transcripts")
    compact = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        threshold=1,
        session_id="session-beta",
    )
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "之前的需求"},
        {"role": "assistant", "content": "之前已经完成一部分"},
        {"role": "user", "content": "请继续处理最新请求"},
    ]
    original_messages = deepcopy(messages)

    compact(messages)

    assert messages == original_messages
    assert len(provider.calls) == 1
    saved_files = list((tmp_path / ".transcripts").glob("session-beta_*.jsonl"))
    assert len(saved_files) == 1


def test_compact_session_id_can_be_rebound_before_saving(tmp_path: Path) -> None:
    provider = StubProvider(
        response=LLMResponse(
            text="压缩摘要",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
        )
    )
    transcript_mgr = TranscriptManager(tmp_path / ".transcripts")
    compact = Compactor(
        provider=provider,
        transcript_mgr=transcript_mgr,
        threshold=1,
        session_id="session-fresh",
    )
    compact.set_session_id("session-restored")
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "之前的需求"},
        {"role": "assistant", "content": "之前已经完成一部分"},
    ]

    compact(messages, force=True)

    assert list((tmp_path / ".transcripts").glob("session-fresh_*.jsonl")) == []
    assert len(list((tmp_path / ".transcripts").glob("session-restored_*.jsonl"))) == 1


def test_transcript_manager_save_and_load_round_trip(tmp_path: Path) -> None:
    manager = TranscriptManager(tmp_path / ".transcripts")
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "你好"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}],
        },
    ]

    saved_path = manager.save(messages, "roundtrip")

    assert saved_path.exists()
    assert manager.load("roundtrip") == messages


def test_get_latest_session_returns_latest_session_id(tmp_path: Path) -> None:
    transcript_dir = tmp_path / ".transcripts"
    transcript_dir.mkdir()
    _write_transcript(
        transcript_dir / "session-old_2026-04-03T09-00-00.jsonl",
        [{"role": "user", "content": "old"}],
    )
    _write_transcript(
        transcript_dir / "session-new_2026-04-03T09-30-00.jsonl",
        [{"role": "user", "content": "new"}],
    )

    manager = TranscriptManager(transcript_dir)

    assert manager.get_latest_session() == "session-new"
    assert manager.list_sessions() == ["session-new", "session-old"]


def test_resume_restores_latest_or_specific_session(tmp_path: Path) -> None:
    transcript_dir = tmp_path / ".transcripts"
    transcript_dir.mkdir()
    older_messages = [{"role": "user", "content": "alpha-v1"}]
    newer_alpha_messages = [{"role": "user", "content": "alpha-v2"}]
    beta_messages = [{"role": "user", "content": "beta"}]
    _write_transcript(
        transcript_dir / "session-alpha_2026-04-03T09-00-00.jsonl",
        older_messages,
    )
    _write_transcript(
        transcript_dir / "session-alpha_2026-04-03T09-20-00.jsonl",
        newer_alpha_messages,
    )
    _write_transcript(
        transcript_dir / "session-beta_2026-04-03T09-40-00.jsonl",
        beta_messages,
    )

    manager = TranscriptManager(transcript_dir)

    assert manager.resume() == beta_messages
    assert manager.resume("session-alpha") == newer_alpha_messages


def test_auto_compact_keeps_messages_when_summary_fails_with_tool_results(tmp_path: Path) -> None:
    """BUG-02 回归：_micro_compact 截断后 provider 失败，消息必须完全不变。"""
    provider = StubProvider(error=RuntimeError("summary failed"))
    compact = Compactor(
        provider=provider,
        transcript_mgr=TranscriptManager(tmp_path / ".transcripts"),
        threshold=1,
        session_id="session-bug02",
    )
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result 1"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "result 2"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "grep", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "result 3"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t4", "name": "glob", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t4", "content": "result 4"}]},
        {"role": "user", "content": "最新请求"},
    ]
    original = deepcopy(messages)

    compact(messages)

    assert messages == original


def _write_transcript(path: Path, messages: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False))
            file.write("\n")
