"""Async message queue for decoupled channel-agent communication."""

import asyncio

from loguru import logger

from nano_alice.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        logger.debug("publish_inbound: channel={}, sender={}", msg.channel, msg.sender_id)
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        msg = await self.inbound.get()
        logger.debug("consume_inbound: channel={}, sender={}", msg.channel, msg.sender_id)
        return msg

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        logger.debug("publish_outbound: channel={}, chat_id={}", msg.channel, msg.chat_id)
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        msg = await self.outbound.get()
        logger.debug("consume_outbound: channel={}, chat_id={}", msg.channel, msg.chat_id)
        return msg

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    def drain_task_continuations(self, task_id: str) -> int:
        """Remove stale task continuation messages for a specific task_id."""
        remaining: list[InboundMessage] = []
        removed = 0
        while not self.inbound.empty():
            try:
                msg = self.inbound.get_nowait()
            except asyncio.QueueEmpty:
                break
            if msg.metadata.get("_task_continue") and str(msg.metadata.get("_task_id", "")) == task_id:
                removed += 1
            else:
                remaining.append(msg)
        for msg in remaining:
            self.inbound.put_nowait(msg)
        if removed:
            logger.debug("Drained {} stale continuations for task_id={}", removed, task_id)
        return removed
