"""TODO service - periodic agent wake-up to check for tasks.

This was refactored from heartbeat/service.py to use SignalBus instead of
direct callbacks. The service emits TODO_CHECK signals when the timer fires.
"""

import asyncio
from pathlib import Path

from loguru import logger

from nano_alice.agent.signals.bus import SignalBus
from nano_alice.agent.signals.types import Signal

# Default interval: 30 minutes
DEFAULT_TODO_INTERVAL_S = 30 * 60

# The prompt sent to agent during TODO check
TODO_PROMPT = """Read TODO.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: TODO_OK"""

# The prompt sent to agent during HEARTBEAT check (legacy support)
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: TODO_OK"""

# Token that indicates "nothing to do"
TODO_OK_TOKEN = "TODO_OK"


def _is_todo_empty(content: str | None) -> bool:
    """Check if TODO.md has no actionable content."""
    if not content:
        return True

    # Lines to skip: empty, headers, HTML comments, empty checkboxes
    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False  # Found actionable content

    return True


class TODOService:
    """
    Periodic TODO service that wakes the agent to check for tasks.

    Changed from HeartbeatService:
    - No callback function - uses SignalBus to emit TODO_CHECK
    - Renamed from Heartbeat to TODO for clarity
    - Checks TODO.md instead of HEARTBEAT.md
    """

    def __init__(
        self,
        workspace: Path,
        signal_bus: SignalBus | None = None,
        interval_s: int = DEFAULT_TODO_INTERVAL_S,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.signal_bus = signal_bus
        self.interval_s = interval_s
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def todo_file(self) -> Path:
        return self.workspace / "TODO.md"

    @property
    def heartbeat_file(self) -> Path:
        """Legacy HEARTBEAT.md support."""
        return self.workspace / "HEARTBEAT.md"

    def _read_todo_file(self) -> str | None:
        """Read TODO.md content."""
        if self.todo_file.exists():
            try:
                return self.todo_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _read_heartbeat_file(self) -> str | None:
        """Read HEARTBEAT.md content (legacy support)."""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def start(self) -> None:
        """Start the TODO service."""
        if not self.enabled:
            logger.info("TODO service disabled")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("TODO service started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the TODO service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main TODO loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("TODO service error: {}", e)

    async def _tick(self) -> None:
        """Execute a single TODO tick."""
        todo_content = self._read_todo_file()
        heartbeat_content = self._read_heartbeat_file()

        # Skip if both files are empty or don't exist
        if _is_todo_empty(todo_content) and _is_todo_empty(heartbeat_content):
            logger.debug("TODO: no tasks (TODO.md and HEARTBEAT.md empty)")
            return

        logger.info("TODO: checking for tasks...")

        # Emit signal instead of calling callback
        if self.signal_bus:
            signal = Signal.todo_check()
            await self.signal_bus.publish(signal)
        else:
            logger.warning("TODO: no signal_bus, check not executed")

    async def trigger_now(self) -> None:
        """Manually trigger a TODO check."""
        if self.signal_bus:
            signal = Signal.todo_check()
            await self.signal_bus.publish(signal)


# Backward compatibility alias
HeartbeatService = TODOService
