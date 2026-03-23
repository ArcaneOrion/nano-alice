"""Heartbeat service for periodic agent wake-ups.

DEPRECATED: This module is renamed to 'todo'. Import from todo instead.
This file remains for backward compatibility during the transition period.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

# Import the new TODO service as base
from nano_alice.todo.service import TODOService


class HeartbeatService(TODOService):
    """
    Legacy-compatible HeartbeatService that wraps the new TODOService.

    Supports the old on_heartbeat callback pattern for backward compatibility.
    When on_heartbeat is set, checks are executed via callback instead of SignalBus.
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
    ):
        # Initialize parent without signal_bus (we use callback instead)
        super().__init__(workspace, signal_bus=None, interval_s=interval_s, enabled=enabled)
        self._legacy_on_heartbeat = on_heartbeat

    @property
    def on_heartbeat(self) -> Callable[[str], Coroutine[Any, Any, str]] | None:
        """Get the legacy callback."""
        return self._legacy_on_heartbeat

    @on_heartbeat.setter
    def on_heartbeat(self, value: Callable[[str], Coroutine[Any, Any, str]] | None) -> None:
        """Set the legacy callback."""
        self._legacy_on_heartbeat = value

    async def _tick(self) -> None:
        """Execute a single heartbeat tick using legacy callback."""
        from loguru import logger

        # Check both TODO.md and HEARTBEAT.md for backward compatibility
        todo_content = self._read_todo_file()
        heartbeat_content = self._read_heartbeat_file()

        # Skip if both files are empty or don't exist
        if self._is_todo_empty(todo_content) and self._is_todo_empty(heartbeat_content):
            logger.debug("Heartbeat: no tasks (TODO.md and HEARTBEAT.md empty)")
            return

        logger.info("Heartbeat: checking for tasks...")

        if self._legacy_on_heartbeat:
            try:
                prompt = self._get_heartbeat_prompt(todo_content, heartbeat_content)
                response = await self._legacy_on_heartbeat(prompt)

                # Check if agent said "nothing to do"
                ok_tokens = ["TODO_OK", "HEARTBEAT_OK"]
                if any(token in response.upper().replace("_", "") for token in ok_tokens):
                    logger.info("Heartbeat: OK (no action needed)")
                else:
                    logger.info("Heartbeat: completed task")

            except Exception as e:
                logger.error("Heartbeat execution failed: {}", e)

    def _is_todo_empty(self, content: str | None) -> bool:
        """Check if content has no actionable items."""
        if not content:
            return True
        skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
                continue
            return False
        return True

    def _get_heartbeat_prompt(self, todo_content: str | None, heartbeat_content: str | None) -> str:
        """Get the appropriate prompt based on which file has content."""
        if heartbeat_content and not self._is_todo_empty(heartbeat_content):
            return """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: HEARTBEAT_OK"""
        return """Read TODO.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: TODO_OK"""

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat using legacy callback."""
        if self._legacy_on_heartbeat:
            todo_content = self._read_todo_file()
            heartbeat_content = self._read_heartbeat_file()
            prompt = self._get_heartbeat_prompt(todo_content, heartbeat_content)
            return await self._legacy_on_heartbeat(prompt)
        return None


__all__ = ["HeartbeatService"]
