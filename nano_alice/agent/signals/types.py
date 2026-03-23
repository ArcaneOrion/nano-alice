"""Signal types for internal agent events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nano_alice.scheduler.types import ScheduledJob


class AgentSignal(Enum):
    """Types of internal signals that trigger Reflect Mode.

    These are NOT user messages - they are internal events that
    require agent attention without creating a conversation session.
    """

    # Time-based signals
    SCHEDULE_TRIGGER = "schedule_trigger"  # Scheduled task (cron) fired
    TODO_CHECK = "todo_check"              # Time to check TODO.md for tasks
    TIMER = "timer"                        # Generic timer event

    # State-based signals
    MEMORY_FULL = "memory_full"            # Memory window exceeded
    SESSION_IDLE = "session_idle"          # No activity for a while

    # Lifecycle signals
    STARTUP = "startup"                    # Agent just started
    SHUTDOWN = "shutdown"                  # Agent shutting down

    # Proactive maintenance
    SELF_REFLECT = "self_reflect"          # Introspection/review time

    # Logging signals
    LOG_ERROR = "log_error"                # Error logged, track system health


@dataclass
class Signal:
    """Internal signal for Reflect Mode processing.

    Signals are fundamentally different from InboundMessage:
    - No channel/chat_id: signals are system-wide
    - No session pollution: processed without creating conversation history
    - Metadata-driven: context comes from signal.data, not user
    """

    type: AgentSignal
    """The type of signal being sent."""

    data: dict[str, Any] = field(default_factory=dict)
    """Signal-specific data (e.g., job_id for SCHEDULE_TRIGGER)."""

    timestamp: datetime = field(default_factory=datetime.now)
    """When the signal was emitted."""

    source: str = "system"
    """Who sent this signal (system, scheduler, todo, etc.)."""

    def with_data(self, **kwargs: Any) -> "Signal":
        """Create a copy with additional data."""
        return Signal(
            type=self.type,
            data={**self.data, **kwargs},
            timestamp=self.timestamp,
            source=self.source,
        )

    @classmethod
    def schedule_trigger(cls, job: ScheduledJob | None = None, session_context: dict[str, str] | None = None) -> "Signal":
        """Create a signal for scheduled task execution."""
        if job is None:
            # For type checking compatibility when no job is available
            return cls(
                type=AgentSignal.SCHEDULE_TRIGGER,
                data={},
                source="scheduler",
            )
        return cls(
            type=AgentSignal.SCHEDULE_TRIGGER,
            data={
                "job_id": job.id,
                "job_name": job.name,
                "message": job.payload.message,
                "deliver": job.payload.deliver,
                "channel": job.payload.channel,
                "to": job.payload.to,
                "session_context": session_context or {},
            },
            source="scheduler",
        )

    @classmethod
    def todo_check(cls) -> "Signal":
        """Create a signal for TODO list checking."""
        return cls(
            type=AgentSignal.TODO_CHECK,
            source="todo",
        )

    @classmethod
    def startup(cls) -> "Signal":
        """Create a signal for agent startup."""
        return cls(
            type=AgentSignal.STARTUP,
            source="system",
        )
