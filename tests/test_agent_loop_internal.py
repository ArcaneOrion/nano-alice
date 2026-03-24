from pathlib import Path

import pytest

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.tools.message import MessageTool
from nano_alice.agent.tools.spawn import SpawnTool
from nano_alice.bus.queue import MessageBus
from nano_alice.providers.base import LLMProvider, LLMResponse
from nano_alice.session.manager import SessionManager


class DummyProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return LLMResponse(content="internal ok")

    def get_default_model(self) -> str:
        return "dummy-model"


class CaptureMessageTool(MessageTool):
    def __init__(self):
        super().__init__()
        self.context_calls = []

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        self.context_calls.append((channel, chat_id, message_id))
        super().set_context(channel, chat_id, message_id)


class CaptureSpawnTool(SpawnTool):
    def __init__(self):
        super().__init__(manager=None)
        self.context_calls = []

    def set_context(self, channel: str, chat_id: str) -> None:
        self.context_calls.append((channel, chat_id))


@pytest.mark.asyncio
async def test_process_internal_does_not_create_or_save_session(tmp_path: Path):
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    response = await agent.process_internal(
        "提醒我喝水",
        channel="telegram",
        chat_id="chat-1",
        event_type="system_event",
        source="scheduler",
    )

    assert response == "internal ok"
    assert sessions.list_sessions() == []
    assert provider.calls, "provider.chat should be called"
    messages = provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "Internal Execution Mode" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Event Type: system_event" in messages[1]["content"]
    assert "Content: 提醒我喝水" in messages[1]["content"]


@pytest.mark.asyncio
async def test_process_internal_sets_tool_context(tmp_path: Path):
    provider = DummyProvider()
    bus = MessageBus()
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
    )
    message_tool = CaptureMessageTool()
    spawn_tool = CaptureSpawnTool()
    agent.tools.register(message_tool)
    agent.tools.register(spawn_tool)

    await agent.process_internal(
        "internal task",
        channel="slack",
        chat_id="room-9",
    )

    assert message_tool.context_calls == [("slack", "room-9", None)]
    assert spawn_tool.context_calls == [("slack", "room-9")]
