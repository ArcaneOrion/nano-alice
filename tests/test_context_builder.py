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
    assert envelope.current_user_input == "latest question"
    assert envelope.history_messages == history
    assert "Current Session" in envelope.system_prompt


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
    assert "<user_input>" in messages[-1]["content"]
    assert "hello world" in messages[-1]["content"]

    metrics = builder.compute_context_metrics(envelope, messages)
    assert metrics["system_chars"] > 0
    assert metrics["current_context_chars"] > 0
    assert metrics["user_input_chars"] == len("hello world")
    assert metrics["history_message_count"] == 0
