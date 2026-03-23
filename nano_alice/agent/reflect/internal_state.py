"""Internal state tracking for the agent.

This tracks agent-internal state that doesn't belong in conversation history:
- Active user session (for scheduler to deliver to right place)
- System health metrics
- Maintenance task status
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InternalState:
    """
    Internal agent state for Reflect Mode processing.

    This is separate from Session (which tracks conversation history)
    and represents the agent's self-awareness of its own state.
    """

    # Current active user context (for scheduler delivery)
    active_channel: str | None = None
    active_chat_id: str | None = None
    active_session_key: str | None = None

    # System health
    last_memory_consolidation: str | None = None  # ISO timestamp
    last_todo_check: str | None = None

    # Maintenance flags
    is_consolidating: bool = False
    pending_tasks: int = 0

    # Custom metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def set_active_session(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set the currently active user session."""
        self.active_channel = channel
        self.active_chat_id = chat_id
        self.active_session_key = session_key

    def get_active_context(self) -> dict[str, str]:
        """Get the active session context for delivery."""
        return {
            "channel": self.active_channel or "cli",
            "chat_id": self.active_chat_id or "direct",
            "session_key": self.active_session_key or "cli:direct",
        }

    def clear_active_session(self) -> None:
        """Clear the active session."""
        self.active_channel = None
        self.active_chat_id = None
        self.active_session_key = None
