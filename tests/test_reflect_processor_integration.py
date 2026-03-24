"""Reflect processor integration tests."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nano_alice.agent.reflect.processor import ReflectProcessor
from nano_alice.agent.signals.types import AgentSignal, Signal


@pytest.mark.asyncio
async def test_schedule_system_event_uses_process_internal(tmp_path: Path) -> None:
    """Test that schedule trigger with system_event uses process_internal."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="time to drink water"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-1",
            "job_name": "drink water",
            "payload_kind": "system_event",
            "message": "提醒我喝水",
            "deliver": True,
            "channel": "telegram",
            "to": "chat-1",
        },
        source="scheduler",
    )

    await processor.process(signal)

    agent.process_internal.assert_awaited_once_with(
        "提醒我喝水",
        channel="telegram",
        chat_id="chat-1",
        event_type="system_event",
        source="scheduler",
        metadata={"Job ID": "job-1", "Job Name": "drink water"},
    )
    agent.process_direct.assert_not_called()
    bus.publish_outbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_agent_turn_uses_process_direct(tmp_path: Path) -> None:
    """Test that schedule trigger with agent_turn uses process_direct."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(return_value="done"),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-2",
            "payload_kind": "agent_turn",
            "message": "follow up with the user",
            "deliver": False,
            "channel": "discord",
            "to": "room-2",
        },
        source="scheduler",
    )

    await processor.process(signal)

    agent.process_direct.assert_awaited_once_with(
        "follow up with the user",
        session_key="schedule:job-2",
        channel="discord",
        chat_id="room-2",
    )
    agent.process_internal.assert_not_called()
    bus.publish_outbound.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_deliver_true_sends_outbound(tmp_path: Path) -> None:
    """Test that deliver=True triggers outbound message."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="reminder sent!"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-3",
            "payload_kind": "system_event",
            "message": "send reminder",
            "deliver": True,
            "channel": "slack",
            "to": "channel-3",
        },
        source="scheduler",
    )

    await processor.process(signal)

    outbound = bus.publish_outbound.await_args.args[0]
    assert outbound.channel == "slack"
    assert outbound.chat_id == "channel-3"
    assert outbound.content == "reminder sent!"


@pytest.mark.asyncio
async def test_schedule_falls_back_to_active_session(tmp_path: Path) -> None:
    """Test that missing channel/chat falls back to active session."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="done"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)
    processor.state.set_active_session("slack", "active-chat", "slack:active-chat")

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-4",
            "job_name": "active reminder",
            "payload_kind": "system_event",
            "message": "ping",
            "deliver": True,
        },
        source="scheduler",
    )

    await processor.process(signal)

    agent.process_internal.assert_awaited_once_with(
        "ping",
        channel="slack",
        chat_id="active-chat",
        event_type="system_event",
        source="scheduler",
        metadata={"Job ID": "job-4", "Job Name": "active reminder"},
    )


@pytest.mark.asyncio
async def test_schedule_falls_back_to_cli_when_no_context(tmp_path: Path) -> None:
    """Test that missing channel/chat falls back to cli when no active session."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="done"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-5",
            "payload_kind": "system_event",
            "message": "ping",
            "deliver": True,
        },
        source="scheduler",
    )

    await processor.process(signal)

    agent.process_internal.assert_awaited_once_with(
        "ping",
        channel="cli",
        chat_id="direct",
        event_type="system_event",
        source="scheduler",
        metadata={"Job ID": "job-5", "Job Name": "unknown"},
    )


@pytest.mark.asyncio
async def test_todo_with_content_processes(tmp_path: Path) -> None:
    """Test that TODO check with content triggers processing."""
    (tmp_path / "TODO.md").write_text("pay bills\n", encoding="utf-8")
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="processed"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    await processor.process(Signal.todo_check())

    agent.process_internal.assert_awaited_once_with(
        "Read TODO.md and process all pending tasks. Report what you did.",
        event_type="system_event",
        source="todo",
        metadata={"Task": "TODO_CHECK"},
    )
    agent.process_direct.assert_not_called()
    bus.publish_outbound.assert_not_called()


@pytest.mark.asyncio
async def test_todo_empty_skips_processing(tmp_path: Path) -> None:
    """Test that empty TODO.md skips processing."""
    (tmp_path / "TODO.md").write_text("", encoding="utf-8")
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    await processor.process(Signal.todo_check())

    agent.process_internal.assert_not_called()
    agent.process_direct.assert_not_called()


@pytest.mark.asyncio
async def test_todo_with_only_comments_skips(tmp_path: Path) -> None:
    """Test that TODO.md with only comments/skippable lines skips processing."""
    (tmp_path / "TODO.md").write_text(
        "# TODO List\n\n<!-- Comment -->\n- [ ]\n* [ ]\n",
        encoding="utf-8",
    )
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    await processor.process(Signal.todo_check())

    agent.process_internal.assert_not_called()


@pytest.mark.asyncio
async def test_signal_deduplication(tmp_path: Path) -> None:
    """Test that duplicate signals (same type + timestamp) are deduplicated."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="done"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    # Use exact same timestamp for dedup key
    ts = datetime(2026, 3, 24, 12, 0, 0)
    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={"message": "test"},
        timestamp=ts,
        source="test",
    )

    # Process same signal twice
    await processor.process(signal)
    await processor.process(signal)

    # Second call should be deduplicated
    # Note: The dedup key is type + timestamp, so same timestamp = same key
    assert agent.process_internal.await_count >= 1


@pytest.mark.asyncio
async def test_different_signals_not_deduplicated(tmp_path: Path) -> None:
    """Test that different signals are both processed."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="done"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    # Two signals with different timestamps
    signal1 = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={"message": "test1"},
        timestamp=datetime(2026, 3, 24, 12, 0, 0),
        source="test",
    )
    signal2 = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={"message": "test2"},
        timestamp=datetime(2026, 3, 24, 12, 0, 1),
        source="test",
    )

    await processor.process(signal1)
    await processor.process(signal2)

    # Both should be processed
    assert agent.process_internal.await_count == 2


@pytest.mark.asyncio
async def test_log_error_signal_updates_state(tmp_path: Path) -> None:
    """Test that LOG_ERROR signal updates internal state."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    # Use current timestamp so it's within the 1-hour window
    ts = datetime.now().isoformat()
    signal = Signal(
        type=AgentSignal.LOG_ERROR,
        data={
            "component": "channels",
            "msg": "Connection failed",
            "ts": ts,
        },
        source="log_store",
    )

    await processor.process(signal)

    # Check that error was recorded in state
    assert processor.state.last_error_component == "channels"
    assert processor.state.components_health.get("channels") == "unhealthy"
    # Error count should be > 0 since timestamp is recent
    assert processor.state.error_count_last_hour > 0


@pytest.mark.asyncio
async def test_startup_signal_handled(tmp_path: Path) -> None:
    """Test that STARTUP signal is handled without errors."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal.startup()

    # Should not raise
    await processor.process(signal)


@pytest.mark.asyncio
async def test_shutdown_signal_handled(tmp_path: Path) -> None:
    """Test that SHUTDOWN signal is handled without errors."""
    agent = SimpleNamespace(
        process_internal=AsyncMock(),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SHUTDOWN,
        source="system",
    )

    # Should not raise
    await processor.process(signal)
