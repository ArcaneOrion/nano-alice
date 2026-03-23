"""Signal bus for internal agent events.

This is a publish-subscribe system for internal signals, separate from
the MessageBus used for user chat messages.
"""

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

from loguru import logger

from nano_alice.agent.signals.types import AgentSignal, Signal


# Signal handler: async function that takes a Signal and returns nothing
SignalHandler = Callable[[Signal], Coroutine[Any, Any, None]]


class SignalBus:
    """
    Internal signal publish-subscribe bus for Reflect Mode.

    This is NOT a replacement for MessageBus - it's a parallel system
    for internal events that should not pollute conversation history.

    Key differences from MessageBus:
    - No queues: signals are dispatched immediately to all subscribers
    - No session tracking: signals are system-wide, not per-user
    - No outbound: signal processing doesn't generate responses
    """

    def __init__(self):
        self._subscribers: dict[AgentSignal, list[SignalHandler]] = defaultdict(list)
        self._running = False
        self._lock = asyncio.Lock()

    def subscribe(self, signal_type: AgentSignal, handler: SignalHandler) -> None:
        """Subscribe to a signal type."""
        self._subscribers[signal_type].append(handler)
        logger.debug("SignalBus: subscribed to {}", signal_type.value)

    def unsubscribe(self, signal_type: AgentSignal, handler: SignalHandler) -> None:
        """Unsubscribe from a signal type."""
        if handler in self._subscribers[signal_type]:
            self._subscribers[signal_type].remove(handler)
            logger.debug("SignalBus: unsubscribed from {}", signal_type.value)

    async def publish(self, signal: Signal) -> None:
        """Publish a signal to all subscribers.

        This is non-blocking - handlers are scheduled but awaited in parallel.
        """
        if not self._running:
            logger.warning("SignalBus: published while not running: {}", signal.type.value)
            return

        handlers = self._subscribers.get(signal.type, [])
        if not handlers:
            logger.debug("SignalBus: no handlers for {}", signal.type.value)
            return

        logger.info("SignalBus: publishing {}", signal.type.value)

        # Run all handlers in parallel and wait for completion
        tasks = [handler(signal) for handler in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any exceptions from handlers
        for result in results:
            if isinstance(result, Exception):
                logger.error("SignalBus: handler failed for {}: {}", signal.type.value, result)

    async def start(self) -> None:
        """Start the signal bus."""
        self._running = True
        logger.info("SignalBus: started")

    def stop(self) -> None:
        """Stop the signal bus."""
        self._running = False
        logger.info("SignalBus: stopped")

    @property
    def is_running(self) -> bool:
        """Check if the signal bus is running."""
        return self._running
