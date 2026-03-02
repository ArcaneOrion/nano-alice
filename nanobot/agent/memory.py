"""Memory system for persistent agent memory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.session.manager import Session


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def append_daily_log(self, entry: str) -> None:
        """Append to today's daily log file (memory/YYYY-MM-DD.md)."""
        from datetime import date

        daily = self.memory_dir / f"{date.today().isoformat()}.md"
        with open(daily, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    async def consolidate(
        self,
        session: Session,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> None:
        """Mark old messages as consolidated (pure trimming, no LLM call)."""
        if archive_all:
            session.last_consolidated = len(session.messages)
            logger.info("Memory consolidation (archive_all): marked {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) > keep_count:
                session.last_consolidated = len(session.messages) - keep_count
                logger.info(
                    "Memory consolidation: trimmed to {} messages, last_consolidated={}",
                    keep_count, session.last_consolidated,
                )
