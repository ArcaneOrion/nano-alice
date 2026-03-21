"""Message bus module for decoupled channel-agent communication."""

from nano_alice.bus.events import InboundMessage, OutboundMessage
from nano_alice.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
