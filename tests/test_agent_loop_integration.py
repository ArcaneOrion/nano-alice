"""Agent loop integration tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.tools.message import MessageTool
from nano_alice.agent.tools.spawn import SpawnTool
from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nano_alice.session.manager import SessionManager


class DummyProvider(LLMProvider):
    """Mock LLM provider for testing."""

    def __init__(self, responses: list[LLMResponse] | None = None):
        super().__init__()
        self.calls: list[dict] = []
        self.responses = responses or [LLMResponse(content="ok")]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.responses:
            response = self.responses.pop(0)
            self.responses.append(response)  # Keep for reuse
            return response
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy-model"


@pytest.mark.asyncio
async def test_agent_single_turn_tool_call(tmp_path: Path) -> None:
    """Test that a single turn with tool call works correctly."""
    # Provider responds with a tool call, then a final message
    tool_response = LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="message",
                arguments={"content": "Hello!"},
            )
        ],
    )
    final_response = LLMResponse(content="Done!")

    provider = DummyProvider(responses=[tool_response, final_response])
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="say hello",
    )

    response = await agent._process_message(msg)

    # When message tool sends, response is None (already sent via bus)
    # So we check that provider was called
    assert len(provider.calls) >= 2
    assert provider.calls[0]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_agent_multi_turn_iterations(tmp_path: Path) -> None:
    """Test that multi-turn tool execution works correctly."""
    # Provider responds with 2 tool calls, then final
    tool1 = LLMResponse(
        content="Thinking...",
        tool_calls=[ToolCallRequest(id="call_1", name="exec", arguments={"command": "echo step1"})],
    )
    tool2 = LLMResponse(
        content="Still thinking...",
        tool_calls=[ToolCallRequest(id="call_2", name="exec", arguments={"command": "echo step2"})],
    )
    final = LLMResponse(content="Complete!")

    provider = DummyProvider(responses=[tool1, tool2, final])
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
        max_iterations=10,
    )

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="do multiple steps",
    )

    response = await agent._process_message(msg)

    # exec tool doesn't send via bus, so we get a response
    if response:
        assert response.content == "Complete!"
    # Check that provider was called multiple times
    assert len(provider.calls) >= 3


@pytest.mark.asyncio
async def test_agent_max_iterations_reached(tmp_path: Path) -> None:
    """Test that agent stops after max iterations."""
    # Provider keeps returning tool calls
    tool_response = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="call_1", name="exec", arguments={"command": "echo loop"})],
    )

    provider = DummyProvider(responses=[tool_response])
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
        max_iterations=3,  # Low limit for testing
    )

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="loop forever",
    )

    response = await agent._process_message(msg)

    # Should return default message after max iterations
    assert response is not None
    assert "no response" in response.content.lower() or "completed" in response.content.lower()


@pytest.mark.asyncio
async def test_process_internal_no_session_pollution(tmp_path: Path) -> None:
    """Test that process_internal doesn't create chat sessions."""
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
        "check system status",
        channel="telegram",
        chat_id="chat-1",
        event_type="system_event",
        source="scheduler",
    )

    assert response == "ok"
    # No session should be created
    assert sessions.list_sessions() == []


@pytest.mark.asyncio
async def test_process_direct_uses_session(tmp_path: Path) -> None:
    """Test that process_direct creates and uses a session."""
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    response = await agent.process_direct(
        "hello",
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
    )

    assert response == "ok"
    # Session should be created (list_sessions returns list of dicts with 'key' field)
    session_keys = [s["key"] for s in sessions.list_sessions()]
    assert "cli:direct" in session_keys


@pytest.mark.asyncio
async def test_agent_saves_session_after_processing(tmp_path: Path) -> None:
    """Test that agent saves session state after processing."""
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="test message",
    )

    await agent._process_message(msg)

    # Session should be saved
    session = sessions.get_or_create("test:chat1")
    # messages is a list of dicts
    assert len(session.messages) == 2  # user + assistant
    assert session.messages[0]["role"] == "user"
    assert session.messages[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_agent_slash_command_new(tmp_path: Path) -> None:
    """Test that /new command clears the session."""
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    # First, create a session with a message
    msg1 = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="first message",
    )
    await agent._process_message(msg1)

    session = sessions.get_or_create("test:chat1")
    assert len(session.messages) > 0

    # Send /new command
    msg2 = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="/new",
    )
    response = await agent._process_message(msg2)

    assert "new session" in response.content.lower()
    session = sessions.get_or_create("test:chat1")
    assert len(session.messages) == 0


@pytest.mark.asyncio
async def test_agent_slash_command_help(tmp_path: Path) -> None:
    """Test that /help command returns help text."""
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="/help",
    )

    response = await agent._process_message(msg)

    assert "nano-alice" in response.content.lower()
    assert "/new" in response.content
    assert "/help" in response.content


@pytest.mark.asyncio
async def test_agent_tool_context_set_on_message(tmp_path: Path) -> None:
    """Test that tool context is set when processing a message."""
    context_calls = []

    class CaptureMessageTool(MessageTool):
        def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
            context_calls.append(("message", channel, chat_id, message_id))
            super().set_context(channel, chat_id, message_id)

    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    # Replace the message tool
    agent.tools.register(CaptureMessageTool(send_callback=bus.publish_outbound))

    msg = InboundMessage(
        channel="telegram",
        sender_id="user1",
        chat_id="chat123",
        content="test",
        metadata={"message_id": "msg456"},
    )

    await agent._process_message(msg)

    # Check that context was set
    assert len(context_calls) > 0
    assert context_calls[0] == ("message", "telegram", "chat123", "msg456")


@pytest.mark.asyncio
async def test_agent_media_attached_to_context(tmp_path: Path) -> None:
    """Test that media files are included in the context."""
    provider = DummyProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )

    # Create a test image file
    test_image = tmp_path / "test.png"
    test_image.write_bytes(b"fake png data")

    msg = InboundMessage(
        channel="test",
        sender_id="user1",
        chat_id="chat1",
        content="describe this",
        media=[str(test_image)],
    )

    await agent._process_message(msg)

    # Check that the message included media
    assert len(provider.calls) >= 1
    messages = provider.calls[0]["messages"]
    # The last user message should contain the media reference
    user_message = [m for m in messages if m["role"] == "user"][-1]
    # Content with media is a list with text and image_url items
    content = user_message["content"]
    assert isinstance(content, list)
    # Should have text and image_url entries
    assert any(isinstance(item, dict) and item.get("type") == "text" for item in content)
    assert any(isinstance(item, dict) and item.get("type") == "image_url" for item in content)
