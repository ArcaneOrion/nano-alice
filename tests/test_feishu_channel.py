import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_alice.agent.loop import AgentLoop
from nano_alice.bus.queue import MessageBus
from nano_alice.channels import feishu as channel_module
from nano_alice.channels.feishu import FeishuChannel
from nano_alice.channels.manager import ChannelManager
from nano_alice.config.schema import Config, FeishuConfig, MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse
from nano_alice.session.manager import SessionManager


class ImmediateLoop:
    async def run_in_executor(self, executor, func, *args):
        return func(*args)


class ReplyProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        return LLMResponse(content="收到你的飞书消息。")

    def get_default_model(self) -> str:
        return "fake/model"


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("facts", encoding="utf-8")
    return workspace


def _make_event(
    *,
    message_id: str,
    content: dict | str,
    message_type: str = "text",
    chat_id: str = "oc_chat_1",
    chat_type: str = "p2p",
    sender_open_id: str = "ou_user_1",
    sender_type: str = "user",
):
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type=message_type,
                content=content,
            ),
            sender=SimpleNamespace(
                sender_type=sender_type,
                sender_id=SimpleNamespace(open_id=sender_open_id),
            ),
        )
    )


def test_feishu_text_message_routes_private_message_to_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        seen_reactions = []

        async def fake_add_reaction(message_id: str, emoji_type: str = "THUMBSUP") -> None:
            seen_reactions.append((message_id, emoji_type))

        monkeypatch.setattr(channel, "_add_reaction", fake_add_reaction)

        await channel._on_message(
            _make_event(
                message_id="om_text_1",
                message_type="text",
                content={"text": "你好，Alice"},
                chat_type="p2p",
                sender_open_id="ou_private_1",
            )
        )

        inbound = await bus.consume_inbound()
        assert inbound.channel == "feishu"
        assert inbound.sender_id == "ou_private_1"
        assert inbound.chat_id == "ou_private_1"
        assert inbound.content == "你好，Alice"
        assert inbound.metadata == {
            "message_id": "om_text_1",
            "chat_type": "p2p",
            "msg_type": "text",
        }
        assert seen_reactions == [("om_text_1", "THUMBSUP")]

    asyncio.run(scenario())


def test_feishu_group_message_keeps_group_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        monkeypatch.setattr(channel, "_add_reaction", lambda *args, **kwargs: asyncio.sleep(0))

        await channel._on_message(
            _make_event(
                message_id="om_group_1",
                message_type="text",
                content={"text": "@alice 站会开始了吗"},
                chat_id="oc_group_1",
                chat_type="group",
                sender_open_id="ou_member_1",
            )
        )

        inbound = await bus.consume_inbound()
        assert inbound.sender_id == "ou_member_1"
        assert inbound.chat_id == "oc_group_1"
        assert inbound.content == "@alice 站会开始了吗"
        assert inbound.metadata["chat_type"] == "group"

    asyncio.run(scenario())


def test_feishu_post_message_extracts_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        monkeypatch.setattr(channel, "_add_reaction", lambda *args, **kwargs: asyncio.sleep(0))

        await channel._on_message(
            _make_event(
                message_id="om_post_1",
                message_type="post",
                content={
                    "zh_cn": {
                        "title": "日报",
                        "content": [[
                            {"tag": "text", "text": "今天完成联调"},
                            {"tag": "at", "user_name": "Alice"},
                            {"tag": "a", "text": "查看详情"},
                        ]],
                    }
                },
            )
        )

        inbound = await bus.consume_inbound()
        assert inbound.content == "日报 今天完成联调 @Alice 查看详情"
        assert inbound.metadata["msg_type"] == "post"

    asyncio.run(scenario())


def test_feishu_interactive_message_extracts_card_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        monkeypatch.setattr(channel, "_add_reaction", lambda *args, **kwargs: asyncio.sleep(0))

        await channel._on_message(
            _make_event(
                message_id="om_card_1",
                message_type="interactive",
                content={
                    "header": {"title": {"content": "审批提醒"}},
                    "elements": [
                        {"tag": "markdown", "content": "请尽快处理"},
                        {
                            "tag": "button",
                            "text": {"content": "打开审批"},
                            "url": "https://example.com/approve",
                        },
                    ],
                },
            )
        )

        inbound = await bus.consume_inbound()
        assert inbound.content == "请尽快处理\n打开审批\nlink: https://example.com/approve\ntitle: 审批提醒"
        assert inbound.metadata["msg_type"] == "interactive"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message_type", "placeholder"),
    [
        ("image", "[image: screenshot.png]"),
        ("file", "[file: design.pdf]"),
        ("audio", "[audio: voice.opus]"),
    ],
)
def test_feishu_media_messages_include_downloaded_file_paths(
    message_type: str,
    placeholder: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        monkeypatch.setattr(channel, "_add_reaction", lambda *args, **kwargs: asyncio.sleep(0))

        async def fake_download_and_save_media(msg_type: str, content_json: dict, message_id: str | None = None):
            names = {
                "image": "screenshot.png",
                "file": "design.pdf",
                "audio": "voice.opus",
            }
            filename = names[msg_type]
            return f"/tmp/{filename}", f"[{msg_type}: {filename}]"

        monkeypatch.setattr(channel, "_download_and_save_media", fake_download_and_save_media)

        payload = {"image_key": "img_1"} if message_type == "image" else {"file_key": "file_1"}
        await channel._on_message(
            _make_event(
                message_id=f"om_{message_type}_1",
                message_type=message_type,
                content=payload,
            )
        )

        inbound = await bus.consume_inbound()
        assert inbound.content == placeholder
        assert inbound.media == [f"/tmp/{placeholder[1:-1].split(': ', 1)[1]}"]
        assert inbound.metadata["msg_type"] == message_type

    asyncio.run(scenario())


def test_feishu_deduplicates_messages_and_ignores_bot_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        monkeypatch.setattr(channel, "_add_reaction", lambda *args, **kwargs: asyncio.sleep(0))

        event = _make_event(
            message_id="om_dup_1",
            message_type="text",
            content={"text": "重复消息"},
        )
        bot_event = _make_event(
            message_id="om_bot_1",
            message_type="text",
            content={"text": "bot 消息"},
            sender_type="bot",
        )

        await channel._on_message(event)
        await channel._on_message(event)
        await channel._on_message(bot_event)

        assert bus.inbound_size == 1
        inbound = await bus.consume_inbound()
        assert inbound.content == "重复消息"
        assert bus.inbound_size == 0

    asyncio.run(scenario())


def test_feishu_registers_and_ignores_message_read_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = MessageBus()
    channel = FeishuChannel(FeishuConfig(), bus)
    registered_handlers = {}

    class FakeBuilder:
        def register_p2_im_message_receive_v1(self, handler):
            registered_handlers["receive"] = handler
            return self

        def register_p2_im_message_message_read_v1(self, handler):
            registered_handlers["message_read"] = handler
            return self

        def build(self):
            return "fake-event-handler"

    monkeypatch.setattr(
        channel_module.lark.EventDispatcherHandler,
        "builder",
        lambda encrypt_key, verification_token: FakeBuilder(),
    )

    event_handler = channel._build_event_handler()

    assert event_handler == "fake-event-handler"
    assert registered_handlers["receive"] == channel._on_message_sync
    assert registered_handlers["message_read"] == channel._on_message_read_sync

    registered_handlers["message_read"](SimpleNamespace(event=SimpleNamespace()))

    assert bus.inbound_size == 0


def test_feishu_end_to_end_conversation_flow_updates_delivery_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace = _make_workspace(tmp_path)
        bus = MessageBus()
        provider = ReplyProvider()
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            session_manager=SessionManager(workspace),
            memory_agent_config=MemoryAgentConfig(enabled=False),
        )
        channel = FeishuChannel(FeishuConfig(), bus)
        channel._client = object()
        sent_messages = []

        async def fake_add_reaction(message_id: str, emoji_type: str = "THUMBSUP") -> None:
            return None

        def fake_send_message_sync(receive_id_type: str, receive_id: str, msg_type: str, content: str) -> str:
            sent_messages.append((receive_id_type, receive_id, msg_type, content))
            return "om_reply_1"

        monkeypatch.setattr(channel, "_add_reaction", fake_add_reaction)
        monkeypatch.setattr(channel, "_send_message_sync", fake_send_message_sync)
        monkeypatch.setattr("nano_alice.channels.feishu.asyncio.get_running_loop", lambda: ImmediateLoop())

        manager = ChannelManager(Config(), bus)
        manager.channels["feishu"] = channel
        dispatch_task = asyncio.create_task(manager._dispatch_outbound())
        try:
            await channel._on_message(
                _make_event(
                    message_id="om_in_1",
                    message_type="text",
                    content={"text": "你好"},
                    sender_open_id="ou_direct_1",
                )
            )

            inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
            response = await loop._process_message(inbound)

            assert response is not None
            assert response.channel == "feishu"
            assert response.chat_id == "ou_direct_1"
            assert response.content == "收到你的飞书消息。"

            await bus.publish_outbound(response)

            receipt_msg = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
            assert receipt_msg.channel == "system"
            assert receipt_msg.sender_id == "delivery"
            assert receipt_msg.metadata["_delivery_receipt"] is True
            assert receipt_msg.metadata["receipt"]["provider_message_id"] == "om_reply_1"

            await loop._process_message(receipt_msg)

            session = loop.sessions.get_or_create("feishu:ou_direct_1")
            assert session.metadata["last_delivery_receipt"]["provider_message_id"] == "om_reply_1"
            assert sent_messages[0][0] == "open_id"
            assert sent_messages[0][1] == "ou_direct_1"
            assert sent_messages[0][2] == "interactive"
        finally:
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_feishu_stop_falls_back_to_internal_disconnect_when_sdk_has_no_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(), bus)
        disconnected = asyncio.Event()
        joined = []

        class FakeWsClient:
            async def _disconnect(self) -> None:
                disconnected.set()

        class FakeThread:
            def __init__(self) -> None:
                self._alive = True

            def is_alive(self) -> bool:
                return self._alive

            def join(self, timeout: float | None = None) -> None:
                joined.append(timeout)
                self._alive = False

        channel._ws_client = FakeWsClient()
        channel._ws_thread = FakeThread()

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr(channel_module.asyncio, "to_thread", fake_to_thread)

        original_loop = getattr(channel_module.lark.ws.client, "loop", None)
        fake_loop = SimpleNamespace(is_running=lambda: False)
        channel_module.lark.ws.client.loop = fake_loop
        try:
            await channel.stop()
        finally:
            channel_module.lark.ws.client.loop = original_loop

        assert disconnected.is_set()
        assert joined == [5.0]
        assert not channel._ws_thread.is_alive()

    asyncio.run(scenario())
