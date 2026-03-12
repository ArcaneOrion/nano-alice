"""Event types for the message bus."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""
    
    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    
    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryReceipt:
    """Structured delivery result emitted by outbound channels."""

    channel: str
    chat_id: str
    status: str
    provider_message_id: str = ""
    error: str = ""
    session_key: str = ""
    intent_id: str = ""
    delivered_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    content_preview: str = ""
    task_id: str = ""
    attachment_names: list[str] = field(default_factory=list)

    def to_metadata(self) -> dict[str, Any]:
        """Convert receipt to message metadata for internal system events."""
        return {"_delivery_receipt": True, "receipt": asdict(self)}
