"""Reflection processor for handling internal signals.

This processes signals in Reflect Mode without polluting conversation history.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nano_alice.agent.reflect.internal_state import InternalState
from nano_alice.agent.signals.types import AgentSignal, Signal

if TYPE_CHECKING:
    from nano_alice.agent.loop import AgentLoop
    from nano_alice.bus.queue import MessageBus


class ReflectProcessor:
    """
    Process internal signals in Reflect Mode.

    Key principles:
    1. Signals do NOT create conversation sessions
    2. Responses are delivered via MessageBus if needed
    3. TODO/Scheduler tasks run in "current user context" if available
    """

    def __init__(
        self,
        agent: AgentLoop,
        bus: MessageBus,
        workspace: Path,
    ):
        self.agent = agent
        self.bus = bus
        self.workspace = workspace
        self.state = InternalState()
        self._processing: set[str] = set()  # Track in-flight signal IDs

    async def process(self, signal: Signal) -> None:
        """Process an incoming signal."""
        # Deduplicate by signal type + timestamp
        sig_key = f"{signal.type.value}:{signal.timestamp.isoformat()}"
        if sig_key in self._processing:
            logger.debug("ReflectProcessor: duplicate signal {}", sig_key)
            return

        self._processing.add(sig_key)

        try:
            match signal.type:
                case AgentSignal.SCHEDULE_TRIGGER:
                    await self._handle_schedule(signal)
                case AgentSignal.TODO_CHECK:
                    await self._handle_todo(signal)
                case AgentSignal.MEMORY_FULL:
                    await self._handle_memory_full(signal)
                case AgentSignal.LOG_ERROR:
                    await self._handle_log_error(signal)
                case AgentSignal.STARTUP:
                    await self._handle_startup(signal)
                case AgentSignal.SHUTDOWN:
                    await self._handle_shutdown(signal)
                case _:
                    logger.debug("ReflectProcessor: unhandled signal {}", signal.type.value)
        finally:
            self._processing.discard(sig_key)

    async def _handle_schedule(self, signal: Signal) -> None:
        """Handle scheduled task execution."""
        job_id = signal.data.get("job_id", "unknown")
        message = signal.data.get("message", "")
        deliver = signal.data.get("deliver", False)
        payload_kind = signal.data.get("payload_kind", "system_event")

        logger.info("ReflectProcessor: executing schedule job {}", job_id)

        # Get delivery context: prefer signal data, then active session
        channel = signal.data.get("channel")
        to = signal.data.get("to")

        if not channel and self.state.active_channel:
            channel = self.state.active_channel
            to = self.state.active_chat_id

        if not channel:
            channel = "cli"
            to = "direct"

        if payload_kind == "agent_turn":
            response = await self.agent.process_direct(
                message,
                session_key=f"schedule:{job_id}",
                channel=channel,
                chat_id=to or "direct",
            )
        else:
            response = await self.agent.process_internal(
                message,
                channel=channel,
                chat_id=to or "direct",
                event_type="system_event",
                source="scheduler",
                metadata={
                    "Job ID": job_id,
                    "Job Name": signal.data.get("job_name", "unknown"),
                },
            )

        # Deliver if requested
        if deliver and response:
            from nano_alice.bus.events import OutboundMessage
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=to or "direct",
                content=response,
            ))

    async def _handle_todo(self, signal: Signal) -> None:
        """Handle TODO list check."""
        logger.info("ReflectProcessor: checking TODO.md")

        todo_file = self.workspace / "TODO.md"
        if not todo_file.exists():
            logger.debug("ReflectProcessor: TODO.md not found")
            return

        content = todo_file.read_text(encoding="utf-8")

        # Check if there are actionable items
        if self._is_todo_empty(content):
            logger.info("ReflectProcessor: TODO is empty, skipping")
            self.state.last_todo_check = signal.timestamp.isoformat()
            return

        # TODO has content - process it
        logger.info("ReflectProcessor: TODO has tasks, processing...")

        response = await self.agent.process_internal(
            "Read TODO.md and process all pending tasks. Report what you did.",
            event_type="system_event",
            source="todo",
            metadata={"Task": "TODO_CHECK"},
        )

        logger.info("ReflectProcessor: TODO processed: {}", response[:100] if response else "no response")
        self.state.last_todo_check = signal.timestamp.isoformat()

    async def _handle_memory_full(self, signal: Signal) -> None:
        """Handle memory consolidation trigger."""
        logger.info("ReflectProcessor: memory full, consolidating...")
        # Memory consolidation is handled by AgentLoop automatically
        # This is just a hook for future enhancements
        self.state.is_consolidating = True

    async def _handle_startup(self, signal: Signal) -> None:
        """Handle agent startup."""
        logger.info("ReflectProcessor: agent startup signal")
        # Could load persistent state here, etc.

    async def _handle_shutdown(self, signal: Signal) -> None:
        """Handle agent shutdown."""
        logger.info("ReflectProcessor: agent shutdown signal")
        # Save state, cleanup, etc.

    async def _handle_log_error(self, signal: Signal) -> None:
        """Handle error log entry for health tracking."""
        component = signal.data.get("component", "unknown")
        msg = signal.data.get("msg", "")
        ts = signal.data.get("ts", signal.timestamp.isoformat())

        logger.debug("ReflectProcessor: recording error from {}", component)

        # Record error in internal state
        self.state.record_error(component, msg, ts)

    @staticmethod
    def _is_todo_empty(content: str) -> bool:
        """Check if TODO.md has actionable content."""
        if not content:
            return True

        skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("<!--"):
                continue
            if line in skip_patterns:
                continue
            return False  # Found actionable content

        return True
