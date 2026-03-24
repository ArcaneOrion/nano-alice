"""End-to-end tests for reminder conversation flow.

These tests simulate real user conversations to ensure:
1. Agent responds naturally when setting reminders
2. Agent understands reminders are its own behavior, not just tool calls
3. No "已设置" or other awkward phrasing from tool results
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.tools.scheduler import SchedulerTool
from nano_alice.bus.queue import MessageBus
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nano_alice.scheduler.service import SchedulerService
from nano_alice.scheduler.types import Schedule
from nano_alice.session.manager import SessionManager


class MockLLMProvider(LLMProvider):
    """Mock provider that simulates natural conversation flow."""

    def __init__(self, response_text: str = "好的，我会提醒你"):
        super().__init__()
        self.response_text = response_text
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append({"messages": messages, "tools": tools})
        # First call: user asks for reminder, Agent should call scheduler tool
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="scheduler",
                        arguments={
                            "action": "add",
                            "message": "喝水时间到了！💧",
                            "at": "2026-03-24T10:00:00",
                        },
                    )
                ],
            )
        # Second call: after tool returns, Agent should respond naturally
        return LLMResponse(content=self.response_text)

    def get_default_model(self) -> str:
        return "mock-model"


@pytest.mark.asyncio
async def test_reminder_conversation_with_observable_result(tmp_path: Path):
    """Test that Agent gets observable job info but still responds naturally."""
    bus = MessageBus()
    provider = MockLLMProvider(response_text="好的，我会提醒你喝水 💧")
    sessions = SessionManager(tmp_path)

    # Create scheduler service
    from nano_alice.agent.signals.bus import SignalBus

    signal_bus = SignalBus()
    scheduler = SchedulerService(tmp_path / "jobs.json", signal_bus=signal_bus)
    await scheduler.start()

    # Create agent loop
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
        scheduler_service=scheduler,
        signal_bus=signal_bus,
    )

    # Set tool context (simulating a feishu channel)
    scheduler_tool = agent.tools.get("scheduler")
    if isinstance(scheduler_tool, SchedulerTool):
        scheduler_tool.set_context("feishu", "user-123")

    # Simulate user message
    from nano_alice.bus.events import InboundMessage

    msg = InboundMessage(
        channel="feishu",
        sender_id="user-123",
        chat_id="user-123",
        content="10点提醒我喝水",
    )

    response = await agent._process_message(msg)

    # Verify Agent responds naturally (not echoing tool result)
    assert response is not None
    assert response.content == "好的，我会提醒你喝水 💧"

    # Verify job was actually created and has an ID
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.message == "喝水时间到了！💧"
    assert jobs[0].id is not None  # Job ID exists for later deletion/debugging


@pytest.mark.asyncio
async def test_reminder_conversation_empty_tool_result(tmp_path: Path):
    """Test that empty tool result doesn't cause awkward responses."""
    bus = MessageBus()
    provider = MockLLMProvider(response_text="没问题！")
    sessions = SessionManager(tmp_path)

    from nano_alice.agent.signals.bus import SignalBus

    signal_bus = SignalBus()
    scheduler = SchedulerService(tmp_path / "jobs2.json", signal_bus=signal_bus)
    await scheduler.start()

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
        scheduler_service=scheduler,
        signal_bus=signal_bus,
    )

    scheduler_tool = agent.tools.get("scheduler")
    if isinstance(scheduler_tool, SchedulerTool):
        scheduler_tool.set_context("telegram", "chat-456")

    from nano_alice.bus.events import InboundMessage

    msg = InboundMessage(
        channel="telegram",
        sender_id="chat-456",
        chat_id="chat-456",
        content="明天早上8点叫我起床",
    )

    response = await agent._process_message(msg)

    # Verify natural response despite empty tool result
    assert response is not None
    assert response.content == "没问题！"
    assert "已设置" not in response.content


@pytest.mark.asyncio
async def test_scheduler_tool_returns_job_info(tmp_path: Path):
    """Test that SchedulerTool.execute returns job info for add action."""
    from nano_alice.agent.signals.bus import SignalBus

    signal_bus = SignalBus()
    scheduler = SchedulerService(tmp_path / "jobs3.json", signal_bus=signal_bus)
    await scheduler.start()

    tool = SchedulerTool(scheduler)
    tool.set_context("feishu", "user-789")

    result = await tool.execute(
        action="add",
        message="test reminder",
        at="2026-03-24T12:00:00",
    )

    # Tool should return job info with id for observability
    assert "Created job" in result
    assert "test reminder" in result
    assert "id:" in result


@pytest.mark.asyncio
async def test_scheduler_tool_list_returns_content(tmp_path: Path):
    """Test that SchedulerTool.execute returns content for list action."""
    from nano_alice.agent.signals.bus import SignalBus

    signal_bus = SignalBus()
    scheduler = SchedulerService(tmp_path / "jobs4.json", signal_bus=signal_bus)
    await scheduler.start()

    # Add a job first
    scheduler.add_job(
        name="test job",
        schedule=Schedule(kind="every", every_ms=1000),
        message="test message",
    )

    tool = SchedulerTool(scheduler)

    result = await tool.execute(action="list")

    # List should return actual content
    assert "Scheduled jobs:" in result
    assert "test job" in result
