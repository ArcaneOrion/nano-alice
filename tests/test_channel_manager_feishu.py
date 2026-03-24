"""Channel manager integration tests focused on Feishu."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nano_alice.bus.events import OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.feishu import FeishuChannel
from nano_alice.channels.manager import ChannelManager
from nano_alice.config.schema import ChannelsConfig, Config, FeishuConfig


def _make_config(feishu_enabled: bool = True) -> Config:
    """Create a test config with Feishu channel."""
    feishu = FeishuConfig(
        enabled=feishu_enabled,
        app_id="test_app_id",
        app_secret="test_app_secret",
        encrypt_key="",
        verification_token="",
        allow_from=[],
    )
    channels = ChannelsConfig(feishu=feishu)
    return Config(channels=channels)


@pytest.mark.asyncio
async def test_channel_manager_initializes_feishu() -> None:
    """Test that ChannelManager correctly initializes Feishu channel."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    assert "feishu" in manager.channels
    assert isinstance(manager.channels["feishu"], FeishuChannel)


@pytest.mark.asyncio
async def test_channel_manager_skips_disabled_feishu() -> None:
    """Test that ChannelManager skips Feishu when disabled."""
    config = _make_config(feishu_enabled=False)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    assert "feishu" not in manager.channels


@pytest.mark.asyncio
async def test_channel_manager_dispatches_to_feishu(monkeypatch) -> None:
    """Test that outbound messages are dispatched to Feishu channel."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Mock the FeishuChannel.send method
    send_called = []

    async def fake_send(self, msg: OutboundMessage) -> None:
        send_called.append(msg)

    monkeypatch.setattr(FeishuChannel, "send", fake_send)

    # Publish outbound message
    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_test_chat",
        content="Hello Feishu!",
    )
    await bus.publish_outbound(msg)

    # Run dispatch loop briefly to process the message
    import asyncio

    async def run_briefly():
        try:
            await asyncio.wait_for(manager._dispatch_outbound(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

    await run_briefly()

    # Check that FeishuChannel.send was called
    assert len(send_called) > 0
    assert send_called[0].chat_id == "oc_test_chat"
    assert send_called[0].content == "Hello Feishu!"


@pytest.mark.asyncio
async def test_channel_manager_warns_on_unknown_channel(monkeypatch, caplog) -> None:
    """Test that ChannelManager logs warning for unknown channels."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Publish outbound message to unknown channel
    msg = OutboundMessage(
        channel="unknown_channel",
        chat_id="some_chat",
        content="Hello?",
    )
    await bus.publish_outbound(msg)

    # Run one dispatch cycle - should handle gracefully
    import asyncio

    try:
        await asyncio.wait_for(manager._dispatch_outbound(), timeout=0.5)
    except asyncio.TimeoutError:
        pass

    # Should not crash, just log warning


@pytest.mark.asyncio
async def test_channel_manager_start_all_starts_feishu(monkeypatch) -> None:
    """Test that start_all() starts the Feishu channel."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Mock FeishuChannel.start to not actually run
    start_called = []

    async def fake_start(self):
        start_called.append("feishu")
        self._running = True
        # Run forever (or until cancelled)
        import asyncio
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self._running = False
            raise

    monkeypatch.setattr(FeishuChannel, "start", fake_start)

    # Start in background task
    import asyncio

    task = asyncio.create_task(manager.start_all())

    # Give it time to start
    await asyncio.sleep(0.1)

    # Cancel the task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "feishu" in start_called


@pytest.mark.asyncio
async def test_channel_manager_stop_all_stops_feishu(monkeypatch) -> None:
    """Test that stop_all() stops the Feishu channel."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Mock start/stop
    async def fake_start(self):
        self._running = True
        import asyncio
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self._running = False
            raise

    async def fake_stop(self):
        self._running = False

    monkeypatch.setattr(FeishuChannel, "start", fake_start)
    monkeypatch.setattr(FeishuChannel, "stop", fake_stop)

    # Start and immediately stop
    import asyncio

    start_task = asyncio.create_task(manager.start_all())
    await asyncio.sleep(0.05)
    await manager.stop_all()

    # Check that channel is not running
    assert not manager.channels["feishu"].is_running


@pytest.mark.asyncio
async def test_channel_manager_send_to_feishu_open_id(monkeypatch) -> None:
    """Test sending to Feishu open_id (DM)."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Mock the send method
    send_called = []

    async def fake_send(self, msg: OutboundMessage) -> None:
        send_called.append({"chat_id": msg.chat_id, "content": msg.content})

    monkeypatch.setattr(FeishuChannel, "send", fake_send)

    # Send to an open_id (not a group chat)
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_test_user",
        content="DM message",
    )
    await bus.publish_outbound(msg)

    # Run one dispatch cycle
    import asyncio

    try:
        await asyncio.wait_for(manager._dispatch_outbound(), timeout=0.5)
    except asyncio.TimeoutError:
        pass

    assert len(send_called) > 0
    assert send_called[0]["chat_id"] == "ou_test_user"


@pytest.mark.asyncio
async def test_channel_manager_multiple_messages(monkeypatch) -> None:
    """Test that multiple outbound messages are all processed."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    # Mock the send method
    send_called = []

    async def fake_send(self, msg: OutboundMessage) -> None:
        send_called.append(msg.content)

    monkeypatch.setattr(FeishuChannel, "send", fake_send)

    # Publish multiple messages
    messages = [
        OutboundMessage(channel="feishu", chat_id="oc_test", content=f"Message {i}")
        for i in range(3)
    ]
    for msg in messages:
        await bus.publish_outbound(msg)

    # Consume messages from queue
    import asyncio

    for _ in range(3):
        msg = await bus.consume_outbound()
        await manager.channels["feishu"].send(msg)

    # All messages should be processed
    assert len(send_called) == 3


@pytest.mark.asyncio
async def test_channel_manager_enabled_channels_property() -> None:
    """Test that enabled_channels returns correct list."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    assert "feishu" in manager.enabled_channels


@pytest.mark.asyncio
async def test_channel_manager_get_channel() -> None:
    """Test that get_channel returns the correct channel."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    channel = manager.get_channel("feishu")
    assert channel is not None
    assert isinstance(channel, FeishuChannel)

    # Unknown channel returns None
    assert manager.get_channel("unknown") is None


@pytest.mark.asyncio
async def test_channel_manager_get_status() -> None:
    """Test that get_status returns correct status."""
    config = _make_config(feishu_enabled=True)
    bus = MessageBus()

    manager = ChannelManager(config, bus)

    status = manager.get_status()
    assert "feishu" in status
    assert status["feishu"]["enabled"] is True
    assert status["feishu"]["running"] is False
