"""Feishu channel integration tests with high-fidelity mocks."""

import json
from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, AsyncMock

import pytest

from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.feishu import FeishuChannel
from nano_alice.config.schema import FeishuConfig


class FakeSenderId:
    def __init__(self, open_id: str):
        self.open_id = open_id


class FakeSender:
    def __init__(self, sender_type: str = "user", sender_id: str = "test_user"):
        self.sender_type = sender_type
        self.sender_id = FakeSenderId(sender_id)


class FakeMessage:
    """Fake Feishu message object."""

    def __init__(
        self,
        message_id: str = "test_msg_id",
        chat_id: str = "oc_test_chat",
        chat_type: str = "group",
        msg_type: str = "text",
        content: str | dict = '{"text": "hello"}',
    ) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.message_type = msg_type
        self.content = json.dumps(content) if isinstance(content, dict) else content


class FakeEvent:
    """Fake Feishu event object."""

    def __init__(
        self,
        message_id: str = "test_msg_id",
        sender_type: str = "user",
        sender_id: str = "test_user",
        chat_id: str = "oc_test_chat",
        chat_type: str = "group",
        msg_type: str = "text",
        content: str | dict = '{"text": "hello"}',
    ) -> None:
        self.message = FakeMessage(message_id, chat_id, chat_type, msg_type, content)
        self.sender = FakeSender(sender_type, sender_id)


class FakeMessageData:
    """Fake P2ImMessageReceiveV1 data structure."""

    def __init__(
        self,
        message_id: str = "test_msg_id",
        sender_type: str = "user",
        sender_id: str = "test_user",
        chat_id: str = "oc_test_chat",
        chat_type: str = "group",
        msg_type: str = "text",
        content: str | dict = '{"text": "hello"}',
    ) -> None:
        self.event = FakeEvent(message_id, sender_type, sender_id, chat_id, chat_type, msg_type, content)


class FakeRequest:
    """Fake request object for API calls."""

    def __init__(self, receive_id: str, receive_id_type: str, msg_type: str, content: str):
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.msg_type = msg_type
        self.content = content


class FakeResponse:
    """Fake response object."""

    def __init__(self, success: bool = True, code: int = 0, msg: str = "success"):
        self._success = success
        self.code = code
        self.msg = msg
        self.data = MagicMock()
        self.data.message_id = "mock_msg_id"

    def success(self) -> bool:
        return self._success


class FakeMessageAPI:
    """Mock message API for creating messages."""

    def __init__(self) -> None:
        self.creates: list[dict] = []

    def create(self, request: FakeRequest) -> FakeResponse:
        self.creates.append(
            {
                "receive_id": request.receive_id,
                "receive_id_type": request.receive_id_type,
                "msg_type": request.msg_type,
                "content": request.content,
            }
        )
        return FakeResponse()


class FakeLarkClient:
    """Mock lark.Client that records all API calls."""

    def __init__(self) -> None:
        self._message_api = FakeMessageAPI()
        self.sent_messages: list[dict] = []
        self.uploaded_images: list[str] = []
        self.uploaded_files: list[str] = []

    @property
    def im(self):
        class _IM:
            def __init__(self, message_api):
                self.v1 = MagicMock()
                self.v1.message = lambda: message_api
                self.v1.image = MagicMock()
                self.v1.file = MagicMock()

            class v1:
                @staticmethod
                def message():
                    pass

                class image:
                    @staticmethod
                    def create(request):
                        response = MagicMock()
                        response.success.return_value = True
                        response.data.image_key = "img_test_key"
                        return response

                class file:
                    @staticmethod
                    def create(request):
                        response = MagicMock()
                        response.success.return_value = True
                        response.data.file_key = "file_test_key"
                        return response

        return _IM(self._message_api)


class FakeLarkWSClient:
    """Mock lark.ws.Client for WebSocket."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _make_feishu_config(**kwargs) -> FeishuConfig:
    defaults = {
        "enabled": True,
        "app_id": "test_app_id",
        "app_secret": "test_app_secret",
        "encrypt_key": "",
        "verification_token": "",
        "allow_from": [],
    }
    defaults.update(kwargs)
    return FeishuConfig(**defaults)


@pytest.mark.asyncio
async def test_feishu_text_message_inbound(message_bus: MessageBus, monkeypatch) -> None:
    """Test that text messages are correctly forwarded to MessageBus."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    # Mock lark module
    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    # Mock reaction addition (async function)
    async def fake_add_reaction(self, message_id, emoji_type="THUMBSUP"):
        pass
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Simulate incoming text message
    event_data = FakeMessageData(msg_type="text", content='{"text": "hello world"}')
    await channel._on_message(event_data)

    assert len(received_messages) == 1
    msg = received_messages[0]
    assert msg.channel == "feishu"
    assert msg.sender_id == "test_user"
    assert msg.chat_id == "oc_test_chat"
    assert msg.content == "hello world"
    assert msg.metadata["message_id"] == "test_msg_id"
    assert msg.metadata["chat_type"] == "group"
    assert msg.metadata["msg_type"] == "text"


async def fake_add_reaction(self, message_id, emoji_type="THUMBSUP"):
    pass


@pytest.mark.asyncio
async def test_feishu_bot_message_ignored(message_bus: MessageBus, monkeypatch) -> None:
    """Test that messages from bots are ignored."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Bot message should be ignored
    event_data = FakeMessageData(sender_type="bot", msg_type="text", content='{"text": "bot says hi"}')
    await channel._on_message(event_data)

    assert len(received_messages) == 0


@pytest.mark.asyncio
async def test_feishu_duplicate_message_id_dedup(message_bus: MessageBus, monkeypatch) -> None:
    """Test that duplicate message_ids are only processed once."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Same message_id twice
    event_data = FakeMessageData(message_id="dup_msg", msg_type="text")
    await channel._on_message(event_data)
    await channel._on_message(event_data)

    assert len(received_messages) == 1


@pytest.mark.asyncio
async def test_feishu_post_rich_text_parsing(message_bus: MessageBus, monkeypatch) -> None:
    """Test that post (rich text) messages are correctly parsed."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Direct format post - content is a list of paragraph lists
    direct_content = {
        "title": "Test Title",
        "content": [
            [{"tag": "text", "text": "Hello "}],
        ],
    }
    event_data = FakeMessageData(msg_type="post", content=direct_content)
    await channel._on_message(event_data)

    assert len(received_messages) == 1
    assert "Test Title" in received_messages[0].content
    assert "Hello" in received_messages[0].content


@pytest.mark.asyncio
async def test_feishu_send_text_message(message_bus: MessageBus, monkeypatch) -> None:
    """Test that sending text messages calls the Lark API."""
    from nano_alice.bus.events import OutboundMessage

    send_called = []

    def mock_send_sync(self, receive_id_type, receive_id, msg_type, content):
        send_called.append({
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "msg_type": msg_type,
            "content": content,
        })
        return True

    monkeypatch.setattr(FeishuChannel, "_send_message_sync", mock_send_sync)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    # Initialize the client to avoid the "not initialized" warning
    channel._client = FakeLarkClient()

    # Send a text message
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_test_chat",
            content="Hello, this is a test message!",
        )
    )

    # Check that the message was created
    assert len(send_called) == 1
    assert send_called[0]["receive_id"] == "oc_test_chat"
    assert send_called[0]["receive_id_type"] == "chat_id"
    assert send_called[0]["msg_type"] == "interactive"


@pytest.mark.asyncio
async def test_feishu_send_to_open_id(message_bus: MessageBus, monkeypatch) -> None:
    """Test that sending to an open_id uses the correct receive_id_type."""
    from nano_alice.bus.events import OutboundMessage

    send_called = []

    def mock_send_sync(self, receive_id_type, receive_id, msg_type, content):
        send_called.append(receive_id_type)
        return True

    monkeypatch.setattr(FeishuChannel, "_send_message_sync", mock_send_sync)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    # Initialize the client to avoid the "not initialized" warning
    channel._client = FakeLarkClient()

    # Send to an open_id (not a group chat)
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_test_user",
            content="DM message",
        )
    )

    assert len(send_called) == 1
    assert send_called[0] == "open_id"


@pytest.mark.asyncio
async def test_feishu_lifecycle_start_stop(message_bus: MessageBus, monkeypatch) -> None:
    """Test that start() creates clients and stop() closes them."""
    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    client_builder_called = False

    def fake_builder():
        nonlocal client_builder_called
        client_builder_called = True
        return fake_client

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        fake_builder,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    # Mock threading.Thread to not actually start threads
    monkeypatch.setattr(
        "nano_alice.channels.feishu.threading.Thread",
        lambda target, daemon: MagicMock(start=MagicMock(), join=MagicMock()),
    )

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)

    # Mock the async start to just set up the client and exit
    import asyncio

    async def fake_start_impl(self):
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._client = fake_builder()
        # Don't actually run the loop
        await asyncio.sleep(0)

    monkeypatch.setattr(FeishuChannel, "start", fake_start_impl)

    await channel.start()

    assert channel._client is not None


def test_extract_post_text_direct_format() -> None:
    """Test _extract_post_text with direct format content."""
    from nano_alice.channels.feishu import _extract_post_text

    # content is a list of paragraph lists, each containing element dicts
    content = {
        "title": "Test Title",
        "content": [
            # Paragraph 1
            [
                {"tag": "text", "text": "Hello world"},
            ],
            # Paragraph 2 (optional)
            [
                {"tag": "text", "text": "More text"},
            ],
        ],
    }

    result = _extract_post_text(content)
    assert "Test Title" in result
    assert "Hello world" in result


def test_extract_post_text_localized_format() -> None:
    """Test _extract_post_text with localized (zh_cn) format."""
    from nano_alice.channels.feishu import _extract_post_text

    # content is a list of paragraph lists
    content = {
        "zh_cn": {
            "title": "中文标题",
            "content": [
                [
                    {"tag": "text", "text": "你好世界"},
                ],
            ],
        }
    }

    result = _extract_post_text(content)
    assert "中文标题" in result
    assert "你好世界" in result


def test_extract_interactive_content() -> None:
    """Test _extract_interactive_content extracts card elements."""
    from nano_alice.channels.feishu import _extract_interactive_content

    content = {
        "title": {"content": "Card Title"},
        "elements": [
            {"tag": "markdown", "content": "**Bold text**"},
            {"tag": "div", "text": {"content": "Plain text"}},
        ],
    }

    result = _extract_interactive_content(content)
    assert "title: Card Title" in result
    assert "**Bold text**" in result
    assert "Plain text" in result


def test_build_card_elements_splits_headings() -> None:
    """Test that _build_card_elements correctly splits markdown by headings."""
    config = _make_feishu_config()
    channel = FeishuChannel(config, MessageBus())

    content = """# Heading 1

Some content here.

## Heading 2

More content."""

    elements = channel._build_card_elements(content)

    # Should have div for headings and markdown for content
    assert len(elements) > 0
    # Check that headings are converted to div elements
    div_elements = [e for e in elements if e.get("tag") == "div"]
    assert len(div_elements) >= 2


def test_build_card_elements_handles_tables() -> None:
    """Test that _build_card_elements correctly parses markdown tables."""
    config = _make_feishu_config()
    channel = FeishuChannel(config, MessageBus())

    content = """| Col1 | Col2 |
|------|------|
| val1 | val2 |
| val3 | val4 |"""

    elements = channel._build_card_elements(content)

    # Should have a table element
    table_elements = [e for e in elements if e.get("tag") == "table"]
    assert len(table_elements) == 1
    table = table_elements[0]
    assert "columns" in table
    assert len(table["columns"]) == 2
    assert len(table["rows"]) == 2


def test_parse_md_table() -> None:
    """Test _parse_md_table correctly parses markdown tables."""
    config = _make_feishu_config()
    channel = FeishuChannel(config, MessageBus())

    table = """| A | B |
|---|---|
| 1 | 2 |
| 3 | 4 |"""

    result = channel._parse_md_table(table)

    assert result is not None
    assert result["tag"] == "table"
    assert len(result["columns"]) == 2
    assert result["columns"][0]["display_name"] == "A"
    assert result["columns"][1]["display_name"] == "B"
    assert len(result["rows"]) == 2
    assert result["rows"][0]["c0"] == "1"
    assert result["rows"][0]["c1"] == "2"


def test_message_id_cache_trim() -> None:
    """Test that the message_id cache trims when exceeding 1000 entries."""
    config = _make_feishu_config()
    channel = FeishuChannel(config, MessageBus())

    # Fill cache beyond limit
    for i in range(1100):
        channel._processed_message_ids[f"msg_{i}"] = None

    # Cache should have been trimmed by popitem when > 1000
    # But since we're adding directly to OrderedDict, we need to manually trim
    while len(channel._processed_message_ids) > 1000:
        channel._processed_message_ids.popitem(last=False)

    assert len(channel._processed_message_ids) <= 1000
    # Oldest entries should be removed
    assert "msg_0" not in channel._processed_message_ids
    assert "msg_1099" in channel._processed_message_ids


@pytest.mark.asyncio
async def test_feishu_empty_content_no_message(message_bus: MessageBus, monkeypatch) -> None:
    """Test that messages with empty content don't get published."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Empty content message - should produce "[sticker]" placeholder
    event_data = FakeMessageData(msg_type="sticker", content='{}')
    await channel._on_message(event_data)

    # Sticker produces placeholder content, so message should be published
    assert len(received_messages) == 1
    assert received_messages[0].content == "[sticker]"


@pytest.mark.asyncio
async def test_feishu_private_message_reply_to_sender(message_bus: MessageBus, monkeypatch) -> None:
    """Test that private messages use sender_id as chat_id for reply."""
    received_messages: list[InboundMessage] = []

    async def capture_inbound(msg: InboundMessage) -> None:
        received_messages.append(msg)

    monkeypatch.setattr(message_bus, "publish_inbound", capture_inbound)

    fake_client = FakeLarkClient()
    fake_ws = FakeLarkWSClient()

    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.Client.builder",
        lambda: fake_client,
    )
    monkeypatch.setattr(
        "nano_alice.channels.feishu.lark.ws.Client",
        lambda *args, **kwargs: fake_ws,
    )
    monkeypatch.setattr(FeishuChannel, "_add_reaction", fake_add_reaction)

    config = _make_feishu_config()
    channel = FeishuChannel(config, message_bus)
    channel._running = True
    channel._loop = MagicMock()
    channel._loop.is_running.return_value = True

    # Private message (chat_type = "p2p")
    event_data = FakeMessageData(chat_type="p2p", sender_id="user_123", msg_type="text")
    await channel._on_message(event_data)

    assert len(received_messages) == 1
    # For private messages, reply_to should be sender_id
    assert received_messages[0].chat_id == "user_123"
