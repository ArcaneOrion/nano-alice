"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json
import uuid
from typing import Any

from loguru import logger

from nano_alice.bus.events import OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.channels.base import BaseChannel
from nano_alice.config.schema import WhatsAppConfig


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"

    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
        self._pending_sends: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_timeout_seconds = 15.0

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.bridge_url

        logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)

                    self._handle_disconnect("WhatsApp bridge connection closed")
                    if self._running:
                        logger.info("WhatsApp bridge connection closed, reconnecting in 5 seconds...")
                        await asyncio.sleep(5)
                        continue

            except asyncio.CancelledError:
                self._handle_disconnect("WhatsApp bridge connection cancelled")
                break
            except Exception as e:
                self._handle_disconnect(f"WhatsApp bridge connection error: {e}")
                logger.warning("WhatsApp bridge connection error: {}", e)

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        self._fail_pending_sends("WhatsApp bridge stopped")

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage):
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return self._failed_receipt(msg, "WhatsApp bridge not connected")

        try:
            request_id = uuid.uuid4().hex
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending_sends[request_id] = future
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content,
                "request_id": request_id,
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            ack = await asyncio.wait_for(future, timeout=self._send_timeout_seconds)
            if ack.get("type") == "sent":
                return self._success_receipt(
                    msg,
                    provider_message_id=str(ack.get("message_id") or request_id),
                )
            error = str(ack.get("error") or "WhatsApp bridge rejected message")
            return self._failed_receipt(msg, error)
        except asyncio.TimeoutError:
            logger.error("WhatsApp bridge send ack timeout for chat {}", msg.chat_id)
            return self._failed_receipt(msg, "WhatsApp bridge send ack timeout")
        except Exception as e:
            logger.error("Error sending WhatsApp message: {}", e)
            return self._failed_receipt(msg, str(e))
        finally:
            self._pending_sends.pop(request_id, None)

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")

            # Extract just the phone number or lid as chat_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info("Sender {}", sender)

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info("Voice message received from {}, but direct download from bridge is not yet supported.", sender_id)
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)
                }
            )

        elif msg_type == "sent":
            request_id = str(data.get("request_id") or "")
            future = self._pending_sends.get(request_id)
            if future and not future.done():
                future.set_result(data)
            else:
                logger.debug("Ignoring unmatched WhatsApp sent ack: {}", request_id or "-")

        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)

            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._handle_disconnect("WhatsApp bridge disconnected")

        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            request_id = str(data.get("request_id") or "")
            future = self._pending_sends.get(request_id)
            if future and not future.done():
                future.set_result(data)
            else:
                logger.error("WhatsApp bridge error: {}", data.get("error"))

    def _fail_pending_sends(self, error: str) -> None:
        for request_id, future in list(self._pending_sends.items()):
            if not future.done():
                future.set_result({"type": "error", "request_id": request_id, "error": error})

    def _handle_disconnect(self, error: str) -> None:
        self._connected = False
        self._ws = None
        self._fail_pending_sends(error)
