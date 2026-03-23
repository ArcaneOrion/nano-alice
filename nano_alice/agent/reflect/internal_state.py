"""Internal state tracking for the agent.

This tracks agent-internal state that doesn't belong in conversation history:
- Active user session (for scheduler to deliver to right place)
- System health metrics
- Maintenance task status
- Error tracking from logs
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

    # Error tracking
    error_count_last_hour: int = 0
    last_error_summary: str | None = None
    last_error_timestamp: str | None = None
    last_error_component: str | None = None
    components_health: dict[str, str] = field(default_factory=dict)
    _error_timestamps: deque[datetime] = field(default_factory=deque, init=False, repr=False)

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

    def record_error(self, component: str, msg: str, timestamp: str) -> None:
        """Record an error from the log system."""
        error_time = datetime.fromisoformat(timestamp)
        self._error_timestamps.append(error_time)
        self._prune_error_window(now=datetime.now())
        self.error_count_last_hour = len(self._error_timestamps)
        self.last_error_summary = f"{component}: {msg[:100]}"
        self.last_error_timestamp = timestamp
        self.last_error_component = component
        self.components_health[component] = "unhealthy"

    def reset_error_count(self) -> None:
        """Reset error count (call periodically)."""
        self._error_timestamps.clear()
        self.error_count_last_hour = 0

    def get_health_status(self, now: datetime | None = None) -> str:
        """Get overall health status."""
        current_time = now or datetime.now()
        self._prune_error_window(now=current_time)
        self.error_count_last_hour = len(self._error_timestamps)
        if self.error_count_last_hour > 10:
            return "critical"
        if self.error_count_last_hour > 5:
            return "degraded"
        return "healthy"

    def _prune_error_window(self, now: datetime) -> None:
        """Keep only error timestamps from the last hour."""
        cutoff = now - timedelta(hours=1)
        while self._error_timestamps and self._error_timestamps[0] < cutoff:
            self._error_timestamps.popleft()
