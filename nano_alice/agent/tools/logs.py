"""Logs tool for agent self-reflection."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from nano_alice.agent.tools.base import Tool
from nano_alice.log import get_log_store
from nano_alice.log.types import Component, LogLevel

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parse_duration(duration: str) -> timedelta:
    """Parse duration string like '30m', '1h', '6h' into timedelta."""
    match = re.match(r"^(\d+)([mhd])$", duration.lower())
    if not match:
        return timedelta(hours=1)  # default

    value = int(match.group(1))
    unit = match.group(2)

    match unit:
        case "m":
            return timedelta(minutes=value)
        case "h":
            return timedelta(hours=value)
        case "d":
            return timedelta(days=value)
        case _:
            return timedelta(hours=1)


def _format_summary(entries: Sequence[LogEntry]) -> str:
    """Format log entries as a summary."""
    if not entries:
        return "No log entries found."

    by_level: dict[LogLevel, int] = {}
    by_event: dict[str, int] = {}
    by_component: dict[Component, int] = {}

    for e in entries:
        by_level[e.level] = by_level.get(e.level, 0) + 1
        by_event[e.event] = by_event.get(e.event, 0) + 1
        by_component[e.component] = by_component.get(e.component, 0) + 1

    lines = [
        f"## Log Summary ({len(entries)} entries)",
        "",
        "### By Level",
    ]
    for level, count in sorted(by_level.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"- {level.value}: {count}")

    lines.extend([
        "",
        "### By Component",
    ])
    for comp, count in sorted(by_component.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"- {comp.value}: {count}")

    lines.extend([
        "",
        "### Top Events",
    ])
    for event, count in sorted(by_event.items(), key=lambda x: x[1], reverse=True)[:10]:
        lines.append(f"- {event}: {count}")

    # Recent errors
    errors = [e for e in entries if e.level == LogLevel.ERROR]
    if errors:
        lines.extend([
            "",
            f"### Recent Errors ({len(errors)})",
        ])
        for e in errors[:5]:
            lines.append(f"- [{e.ts.strftime('%H:%M:%S')}] {e.component.value}: {e.msg[:80]}")

    return "\n".join(lines)


def _format_entries(entries: Sequence[LogEntry]) -> str:
    """Format log entries as detailed list."""
    if not entries:
        return "No log entries found."

    lines = [f"## Log Entries ({len(entries)})"]
    for e in entries[:50]:  # limit display
        level_icon = {
            LogLevel.DEBUG: "🔍",
            LogLevel.INFO: "ℹ️",
            LogLevel.WARNING: "⚠️",
            LogLevel.ERROR: "❌",
        }.get(e.level, "•")

        data_str = ""
        if e.data:
            data_str = f" | {e.data}"

        lines.append(
            f"{level_icon} [{e.ts.strftime('%H:%M:%S')}] "
            f"{e.component.value}/{e.event}: {e.msg}{data_str}"
        )

    if len(entries) > 50:
        lines.append(f"\n... and {len(entries) - 50} more entries")

    return "\n".join(lines)


class LogsTool(Tool):
    """Query system logs for agent self-reflection."""

    name = "logs"
    description = (
        "查询系统日志，了解运行状态和错误信息。"
        "可以按组件、级别、时间窗口过滤，支持摘要或详细模式。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "enum": ["agent", "channels", "scheduler", "signals", "reflect", "tools"],
                "description": "过滤特定组件的日志",
            },
            "level": {
                "type": "string",
                "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "description": "过滤特定日志级别",
            },
            "last": {
                "type": "string",
                "description": "时间窗口，如 30m、1h、6h、1d",
                "default": "1h",
            },
            "summarize": {
                "type": "boolean",
                "description": "返回摘要而非详细条目",
                "default": True,
            },
            "limit": {
                "type": "integer",
                "description": "最大返回条数",
                "default": 50,
                "minimum": 1,
                "maximum": 500,
            },
        },
    }

    async def execute(
        self,
        component: str | None = None,
        level: str | None = None,
        last: str = "1h",
        summarize: bool = True,
        limit: int = 50,
    ) -> str:
        """Execute the logs query."""
        store = get_log_store()
        if not store:
            return "LogStore not initialized. Please ensure logging is enabled."

        # Parse component
        comp_filter: Component | None = None
        if component:
            try:
                comp_filter = Component(component)
            except ValueError:
                return f"Invalid component: {component}. Valid: {', '.join(c.value for c in Component)}"

        # Parse level
        level_filter: LogLevel | None = None
        if level:
            try:
                level_filter = LogLevel[level]
            except KeyError:
                return f"Invalid level: {level}. Valid: DEBUG, INFO, WARNING, ERROR"

        # Parse time window
        since = datetime.now() - _parse_duration(last)

        # Query
        entries = store.query(
            component=comp_filter,
            level=level_filter,
            since=since,
            limit=min(limit, 500),
        )

        # Format
        if summarize:
            return _format_summary(entries)
        return _format_entries(entries)
