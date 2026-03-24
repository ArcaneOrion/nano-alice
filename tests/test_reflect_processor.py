from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nano_alice.agent.reflect.processor import ReflectProcessor
from nano_alice.agent.signals.types import AgentSignal, Signal


@pytest.mark.asyncio
async def test_schedule_system_event_uses_process_internal_and_delivers(tmp_path: Path):
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
    outbound = bus.publish_outbound.await_args.args[0]
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "chat-1"
    assert outbound.content == "time to drink water"


@pytest.mark.asyncio
async def test_schedule_agent_turn_uses_process_direct_without_delivery(tmp_path: Path):
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
async def test_schedule_falls_back_to_active_context(tmp_path: Path):
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
            "job_id": "job-3",
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
        metadata={"Job ID": "job-3", "Job Name": "active reminder"},
    )
    outbound = bus.publish_outbound.await_args.args[0]
    assert outbound.channel == "slack"
    assert outbound.chat_id == "active-chat"


@pytest.mark.asyncio
async def test_schedule_falls_back_to_cli_direct_without_context(tmp_path: Path):
    agent = SimpleNamespace(
        process_internal=AsyncMock(return_value="done"),
        process_direct=AsyncMock(),
    )
    bus = SimpleNamespace(publish_outbound=AsyncMock())
    processor = ReflectProcessor(agent, bus, tmp_path)

    signal = Signal(
        type=AgentSignal.SCHEDULE_TRIGGER,
        data={
            "job_id": "job-4",
            "job_name": "fallback reminder",
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
        metadata={"Job ID": "job-4", "Job Name": "fallback reminder"},
    )
    outbound = bus.publish_outbound.await_args.args[0]
    assert outbound.channel == "cli"
    assert outbound.chat_id == "direct"


@pytest.mark.asyncio
async def test_todo_uses_process_internal(tmp_path: Path):
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
