import asyncio
import json

from nano_alice.bus.events import OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.whatsapp import WhatsAppChannel
from nano_alice.config.schema import WhatsAppConfig


class FakeWebSocket:
    def __init__(self, channel: WhatsAppChannel, *, ack_type: str = "sent", error: str = ""):
        self.channel = channel
        self.ack_type = ack_type
        self.error = error

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        ack = {
            "type": self.ack_type,
            "request_id": payload["request_id"],
        }
        if self.ack_type == "error":
            ack["error"] = self.error or "bridge failed"
        else:
            ack["message_id"] = "wa-msg-1"
        asyncio.create_task(self.channel._handle_bridge_message(json.dumps(ack)))


def test_whatsapp_send_waits_for_sent_ack() -> None:
    async def scenario() -> None:
        channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
        channel._connected = True
        channel._ws = FakeWebSocket(channel, ack_type="sent")
        receipt = await channel.send(
            OutboundMessage(channel="whatsapp", chat_id="user@s.whatsapp.net", content="提醒一下")
        )
        assert receipt.status == "sent"
        assert receipt.provider_message_id == "wa-msg-1"

    asyncio.run(scenario())


def test_whatsapp_send_returns_failed_receipt_on_error_ack() -> None:
    async def scenario() -> None:
        channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
        channel._connected = True
        channel._ws = FakeWebSocket(channel, ack_type="error", error="device offline")
        receipt = await channel.send(
            OutboundMessage(channel="whatsapp", chat_id="user@s.whatsapp.net", content="提醒一下")
        )
        assert receipt.status == "failed"
        assert receipt.error == "device offline"

    asyncio.run(scenario())
