import asyncio

import pytest

from nano_alice.bus.events import OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.feishu import FeishuChannel
from nano_alice.config.schema import FeishuConfig


class ImmediateLoop:
    async def run_in_executor(self, executor, func, *args):
        return func(*args)


def test_feishu_send_returns_failed_receipt_when_api_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        channel = FeishuChannel(FeishuConfig(), MessageBus())
        channel._client = object()
        channel._send_message_sync = lambda *args: None
        monkeypatch.setattr("nano_alice.channels.feishu.asyncio.get_running_loop", lambda: ImmediateLoop())
        receipt = await channel.send(
            OutboundMessage(channel="feishu", chat_id="chat1", content="提醒一下")
        )
        assert receipt.status == "failed"
        assert "rejected" in receipt.error

    asyncio.run(scenario())


def test_feishu_send_returns_success_receipt_with_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        channel = FeishuChannel(FeishuConfig(), MessageBus())
        channel._client = object()
        channel._send_message_sync = lambda *args: "om_123"
        monkeypatch.setattr("nano_alice.channels.feishu.asyncio.get_running_loop", lambda: ImmediateLoop())
        receipt = await channel.send(
            OutboundMessage(channel="feishu", chat_id="chat1", content="提醒一下")
        )
        assert receipt.status == "sent"
        assert receipt.provider_message_id == "om_123"

    asyncio.run(scenario())
