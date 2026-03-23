"""Signal system for internal agent events.

This module provides a publish-subscribe pattern for internal signals
that are separate from user chat messages. Signals are used for
self-reflection, scheduled tasks, and system maintenance.

Key difference from MessageBus:
- MessageBus: User messages from external channels → Chat Mode
- SignalBus: Internal signals → Reflect Mode

This separation prevents internal maintenance from polluting conversation history.
"""

from nano_alice.agent.signals.bus import SignalBus
from nano_alice.agent.signals.types import AgentSignal, Signal

__all__ = ["AgentSignal", "Signal", "SignalBus"]
