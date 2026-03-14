from pathlib import Path

from nano_alice.agent.context import ContextBuilder


def test_build_prompt_envelope_separates_context_and_user_input(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("remember this", encoding="utf-8")

    builder = ContextBuilder(workspace)
    history = [
        {"role": "user", "content": "[2026-03-06 13:00] hi"},
        {"role": "assistant", "content": "hello"},
    ]

    envelope = builder.build_prompt_envelope(
        history=history,
        current_message="latest question",
        recalled_context="recalled fact",
        channel="feishu",
        chat_id="chat-1",
    )

    assert "recalled fact" in envelope.current_context_text
    assert "stable identity" not in envelope.current_context_text
    assert "<current_time>" in envelope.current_context_text
    assert envelope.current_user_input == "latest question"
    assert envelope.history_messages == history
    assert "Current Session" in envelope.system_prompt
    assert "## Current Time" not in envelope.system_prompt


def test_render_messages_wraps_current_turn_context_and_input(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "USER.md").write_text("user prefs", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("remember this", encoding="utf-8")

    builder = ContextBuilder(workspace)
    envelope = builder.build_prompt_envelope(
        history=[],
        current_message="hello world",
        recalled_context="memory snippet",
    )

    messages = builder.render_messages(envelope)

    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"
    assert "<context>" in messages[-1]["content"]
    assert "<current_time>" in messages[-1]["content"]
    assert "<user_input>" in messages[-1]["content"]
    assert "hello world" in messages[-1]["content"]

    metrics = builder.compute_context_metrics(envelope, messages)
    assert metrics["system_chars"] > 0
    assert metrics["current_context_chars"] > 0
    assert metrics["user_input_chars"] == len("hello world")
    assert metrics["history_message_count"] == 0


def test_system_prompt_is_stable_across_repeated_builds(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("remember this", encoding="utf-8")

    builder = ContextBuilder(workspace)

    first = builder.build_prompt_envelope(history=[], current_message="one")
    second = builder.build_prompt_envelope(history=[], current_message="two")

    assert first.system_prompt == second.system_prompt
    assert "<current_time>" in first.current_context_text
    assert "<current_time>" in second.current_context_text


def test_render_messages_can_encode_internal_event_without_user_input(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("remember this", encoding="utf-8")

    builder = ContextBuilder(workspace)
    envelope = builder.build_prompt_envelope(
        history=[],
        current_message="",
        recalled_context="memory snippet",
        delivery_summary="source=system\nstatus=sent",
        internal_event_text="source=system\nevent_type=task_continue",
    )

    messages = builder.render_messages(envelope)
    user_content = messages[-1]["content"]

    assert "<internal_event>" in user_content
    assert "event_type=task_continue" in user_content
    assert "<delivery>" in user_content
    assert "status=sent" in user_content
    assert "<user_input>" not in user_content

    metrics = builder.compute_context_metrics(envelope, messages)
    assert metrics["user_input_chars"] == 0


def test_add_tool_result_truncates_web_fetch_payload(tmp_path: Path) -> None:
    import json

    workspace = tmp_path
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("remember this", encoding="utf-8")

    builder = ContextBuilder(workspace)
    messages: list[dict] = [{"role": "system", "content": "sys"}]

    original_text = "a" * (builder.WEB_FETCH_TEXT_MAX_CHARS + 5000)
    result = json.dumps({"url": "https://example.com", "text": original_text}, ensure_ascii=False)
    updated = builder.add_tool_result(messages, "call-1", "web_fetch", result)

    tool_message = updated[-1]
    assert tool_message["role"] == "tool"
    payload = json.loads(tool_message["content"])
    assert payload["text_truncated_for_context"] is True
    assert payload["text_full_length"] == len(original_text)
    assert len(payload["text"]) <= builder.WEB_FETCH_TEXT_MAX_CHARS + 64
