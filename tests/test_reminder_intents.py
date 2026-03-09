import asyncio
from pathlib import Path

from nano_alice.agent.loop import AgentLoop
from nano_alice.bus.events import DeliveryReceipt, InboundMessage, OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.base import BaseChannel
from nano_alice.channels.manager import ChannelManager
from nano_alice.config.schema import Config, MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse
from nano_alice.session.manager import SessionManager


class IntentProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        return LLMResponse(content="这是一次内部提醒。")

    def get_default_model(self) -> str:
        return "fake/model"


class EmptyIntentProvider(IntentProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        return LLMResponse(content="")


class FailingIntentProvider(IntentProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        raise RuntimeError("llm down")


class FakeChannel(BaseChannel):
    name = "fake"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> DeliveryReceipt:
        return self._success_receipt(msg, provider_message_id="msg-123")


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("facts", encoding="utf-8")
    return workspace


def test_due_intent_event_generates_outbound_without_session_history(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = IntentProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    intent = loop.reminder_intents.create(
        session_key="feishu:chat1",
        origin_channel="feishu",
        origin_chat_id="chat1",
        goal="提醒用户喝水",
        why_notify="到点喝水",
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="cron",
                chat_id="feishu:chat1",
                content="",
                metadata={
                    "_cron_intent_due": True,
                    "_intent_id": intent.intent_id,
                    "_session_key": "feishu:chat1",
                },
            )
        )
    )

    assert response is not None
    assert response.channel == "feishu"
    assert response.chat_id == "chat1"
    assert response.metadata["_intent_id"] == intent.intent_id
    assert "Internal Reminder Intent Due" in provider.calls[0][-1]["content"]
    assert loop.sessions.get_or_create("feishu:chat1").messages == []


def test_delivery_receipt_updates_intent_and_session_metadata(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=IntentProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    intent = loop.reminder_intents.create(
        session_key="feishu:chat1",
        origin_channel="feishu",
        origin_chat_id="chat1",
        goal="提醒用户喝水",
        why_notify="到点喝水",
    )

    asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="delivery",
                chat_id="feishu:chat1",
                content="",
                metadata={
                    "_delivery_receipt": True,
                    "receipt": {
                        "channel": "feishu",
                        "chat_id": "chat1",
                        "status": "sent",
                        "provider_message_id": "om_123",
                        "session_key": "feishu:chat1",
                        "intent_id": intent.intent_id,
                        "content_preview": "提醒内容",
                    },
                },
            )
        )
    )

    updated = loop.reminder_intents.load(intent.intent_id)
    assert updated is not None
    assert updated.delivery_state.status == "sent"
    assert updated.delivery_state.last_message_id == "om_123"
    session = loop.sessions.get_or_create("feishu:chat1")
    assert session.metadata["last_delivery_receipt"]["provider_message_id"] == "om_123"


def test_delivery_receipt_preserves_skipped_status(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=IntentProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    intent = loop.reminder_intents.create(
        session_key="feishu:chat1",
        origin_channel="feishu",
        origin_chat_id="chat1",
        goal="提醒用户喝水",
        why_notify="到点喝水",
    )

    asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="delivery",
                chat_id="feishu:chat1",
                content="",
                metadata={
                    "_delivery_receipt": True,
                    "receipt": {
                        "channel": "feishu",
                        "chat_id": "chat1",
                        "status": "skipped",
                        "session_key": "feishu:chat1",
                        "intent_id": intent.intent_id,
                        "content_preview": "提醒内容",
                    },
                },
            )
        )
    )

    updated = loop.reminder_intents.load(intent.intent_id)
    assert updated is not None
    assert updated.delivery_state.status == "skipped"


def test_due_intent_event_marks_skipped_when_llm_returns_empty(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=EmptyIntentProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    intent = loop.reminder_intents.create(
        session_key="feishu:chat1",
        origin_channel="feishu",
        origin_chat_id="chat1",
        goal="提醒用户喝水",
        why_notify="到点喝水",
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="cron",
                chat_id="feishu:chat1",
                content="",
                metadata={
                    "_cron_intent_due": True,
                    "_intent_id": intent.intent_id,
                    "_session_key": "feishu:chat1",
                },
            )
        )
    )

    updated = loop.reminder_intents.load(intent.intent_id)
    assert response is None
    assert updated is not None
    assert updated.last_notified_at != ""
    assert updated.delivery_state.status == "skipped"


def test_due_intent_event_marks_failed_when_llm_raises(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=FailingIntentProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    intent = loop.reminder_intents.create(
        session_key="feishu:chat1",
        origin_channel="feishu",
        origin_chat_id="chat1",
        goal="提醒用户喝水",
        why_notify="到点喝水",
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="cron",
                chat_id="feishu:chat1",
                content="",
                metadata={
                    "_cron_intent_due": True,
                    "_intent_id": intent.intent_id,
                    "_session_key": "feishu:chat1",
                },
            )
        )
    )

    updated = loop.reminder_intents.load(intent.intent_id)
    assert response is None
    assert updated is not None
    assert updated.last_notified_at == ""
    assert updated.delivery_state.status == "failed"
    assert updated.delivery_state.last_error == "llm down"


def test_channel_manager_publishes_delivery_receipt_event() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        manager = ChannelManager(Config(), bus)
        manager.channels["fake"] = FakeChannel(object(), bus)
        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            await bus.publish_outbound(
                OutboundMessage(
                    channel="fake",
                    chat_id="chat1",
                    content="hello",
                    metadata={"_session_key": "fake:chat1", "_intent_id": "intent_1"},
                )
            )
            receipt_msg = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
            assert receipt_msg.channel == "system"
            assert receipt_msg.sender_id == "delivery"
            assert receipt_msg.metadata["_delivery_receipt"] is True
            assert receipt_msg.metadata["receipt"]["provider_message_id"] == "msg-123"
            assert receipt_msg.metadata["receipt"]["intent_id"] == "intent_1"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
