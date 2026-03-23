"""Structured logging for nano-alice."""

from __future__ import annotations

import sys

from loguru import logger

from nano_alice.log.store import LogStore, _FileSink
from nano_alice.log.types import Component, LogEntry, LogLevel

__all__ = [
    "Component",
    "LogEntry",
    "LogLevel",
    "LogStore",
    "ensure_logging_initialized",
    "get_log_store",
    "set_console_level",
]

_logging_initialized = False
_log_store: LogStore | None = None
_console_sink_id: int | None = None


def _infer_component(record: dict) -> Component:
    """Infer component from loguru record name.

    Matching order: more specific paths first.
    """
    name: str = record.get("name", "")

    if name.startswith("nano_alice.agent.signals"):
        return Component.SIGNALS
    if name.startswith("nano_alice.agent.reflect"):
        return Component.REFLECT
    if name.startswith("nano_alice.agent.tools"):
        return Component.TOOLS
    if name.startswith("nano_alice.channels"):
        return Component.CHANNELS
    if name.startswith("nano_alice.scheduler") or name.startswith("nano_alice.cron"):
        return Component.SCHEDULER
    return Component.AGENT


def ensure_logging_initialized(retention_hours: int = 6) -> LogStore:
    """Ensure logging is initialized (idempotent).

    Sets up console sink (level-controlled) + file sink (always writes).
    """
    global _logging_initialized, _log_store, _console_sink_id

    if _logging_initialized:
        return _log_store  # type: ignore[return-value]

    from nano_alice.utils.helpers import get_logs_path

    log_dir = get_logs_path()
    _log_store = LogStore(log_dir, retention_hours)

    logger.remove()

    # Console sink: stderr output, save ID for dynamic level control
    _console_sink_id = logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level="INFO",
    )

    # File sink: structured JSONL, nano_alice namespace only
    logger.add(
        _FileSink(_log_store),
        filter=lambda r: r["name"].startswith("nano_alice"),
        level="DEBUG",
    )

    _logging_initialized = True
    return _log_store


def set_console_level(level: str = "INFO") -> None:
    """Dynamically adjust the console sink log level.

    Removes and re-adds the console sink without affecting the file sink.

    Args:
        level: Log level such as "DEBUG", "INFO", "WARNING", "ERROR", "SUCCESS"

    Example:
        set_console_level("WARNING")  # Only show WARNING and above
        set_console_level("INFO")     # Show INFO and above (default)
    """
    global _console_sink_id

    if _console_sink_id is not None:
        logger.remove(_console_sink_id)

    _console_sink_id = logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=level,
    )


def get_log_store() -> LogStore | None:
    """Get the LogStore singleton."""
    return _log_store
