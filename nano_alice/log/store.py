"""LogStore and FileSink for structured log persistence."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nano_alice.log.types import Component, LogEntry, LogLevel

if TYPE_CHECKING:
    from nano_alice.agent.signals.bus import SignalBus

# loguru level name -> LogLevel mapping
_LEVEL_MAP: dict[str, LogLevel] = {
    "TRACE": LogLevel.DEBUG,
    "DEBUG": LogLevel.DEBUG,
    "INFO": LogLevel.INFO,
    "SUCCESS": LogLevel.INFO,
    "WARNING": LogLevel.WARNING,
    "ERROR": LogLevel.ERROR,
    "CRITICAL": LogLevel.ERROR,
}


class LogStore:
    """Structured log storage with per-component JSONL files."""

    _cleanup_threshold = 100

    def __init__(self, log_dir: Path, retention_hours: int = 6):
        self._dir = log_dir
        self._retention = timedelta(hours=retention_hours)
        self._signal_bus: SignalBus | None = None
        self._cleanup_counters: dict[Component, int] = {c: 0 for c in Component}
        self._locks: dict[Component, threading.Lock] = {c: threading.Lock() for c in Component}

    def set_signal_bus(self, bus: SignalBus) -> None:
        self._signal_bus = bus

    def write(self, entry: LogEntry) -> None:
        try:
            self._append(entry)
        except Exception as exc:
            print(f"[nano-alice log] write failed: {exc}", file=sys.stderr)
            return

        self._cleanup_counters[entry.component] += 1
        if self._cleanup_counters[entry.component] >= self._cleanup_threshold:
            self._cleanup_counters[entry.component] = 0
            try:
                self._cleanup(entry.component)
            except Exception as exc:
                print(f"[nano-alice log] cleanup failed: {exc}", file=sys.stderr)

        # Publish error signal if this is an ERROR level entry
        if entry.level == LogLevel.ERROR and self._signal_bus:
            self._publish_error_signal(entry)

    def _append(self, entry: LogEntry) -> None:
        lock = self._locks[entry.component]
        path = self._dir / f"{entry.component.value}.jsonl"
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry.to_jsonl() + "\n")

    def _cleanup(self, component: Component) -> None:
        cutoff = datetime.now() - self._retention
        path = self._dir / f"{component.value}.jsonl"

        if not path.exists():
            return

        lock = self._locks[component]
        with lock:
            valid_lines: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LogEntry.from_jsonl(line)
                    if entry.ts > cutoff:
                        valid_lines.append(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

            temp_path = path.with_suffix(".jsonl.tmp")
            temp_path.write_text(
                "\n".join(valid_lines) + ("\n" if valid_lines else ""),
                encoding="utf-8",
            )
            temp_path.replace(path)

    def query(
        self,
        component: Component | None = None,
        level: LogLevel | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        results: list[LogEntry] = []

        for path in self._files_for_component(component):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LogEntry.from_jsonl(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if level and entry.level != level:
                    continue
                if since and entry.ts < since:
                    continue

                results.append(entry)

        results.sort(key=lambda e: e.ts, reverse=True)
        return results[:limit]

    def summarize(self, component: Component | None = None) -> dict:
        entries = self.query(component, limit=10000)
        by_event: dict[str, int] = {}
        for e in entries:
            by_event[e.event] = by_event.get(e.event, 0) + 1
        return {
            "total": len(entries),
            "errors": sum(1 for e in entries if e.level == LogLevel.ERROR),
            "warnings": sum(1 for e in entries if e.level == LogLevel.WARNING),
            "by_event": by_event,
        }

    def _files_for_component(self, component: Component | None) -> list[Path]:
        if component:
            return [self._dir / f"{component.value}.jsonl"]
        return [self._dir / f"{c.value}.jsonl" for c in Component]

    def _publish_error_signal(self, entry: LogEntry) -> None:
        """Publish a LOG_ERROR signal to the signal bus."""
        if not self._signal_bus:
            return

        try:
            # Import here to avoid circular dependency
            from nano_alice.agent.signals.types import AgentSignal, Signal

            signal = Signal(
                type=AgentSignal.LOG_ERROR,
                data={
                    "component": entry.component.value,
                    "msg": entry.msg,
                    "ts": entry.ts.isoformat(),
                },
                source="log_store",
            )
            # Schedule signal publication (non-blocking)
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._signal_bus.publish(signal))
            except RuntimeError:
                # No running loop, publish synchronously (shouldn't happen in normal operation)
                pass
        except Exception as exc:
            print(f"[nano-alice log] failed to publish error signal: {exc}", file=sys.stderr)


class _FileSink:
    """Loguru custom sink that writes structured entries to LogStore."""

    def __init__(self, store: LogStore):
        self._store = store

    def write(self, message) -> None:
        try:
            record = message.record

            level_name = record["level"].name
            log_level = _LEVEL_MAP.get(level_name, LogLevel.INFO)

            from nano_alice.log import _infer_component

            extra = record.get("extra", {})
            event = extra.get("event", "log")
            data = extra.get("data", {})

            # Ensure data is JSON-serializable
            if data and not isinstance(data, dict):
                data = {"raw": str(data)}
            if isinstance(data, dict):
                safe_data = {}
                for k, v in data.items():
                    try:
                        json.dumps(v)
                        safe_data[k] = v
                    except (TypeError, ValueError):
                        safe_data[k] = str(v)
                data = safe_data

            entry = LogEntry(
                ts=datetime.fromtimestamp(record["time"].timestamp()),
                level=log_level,
                component=_infer_component(record),
                event=event,
                msg=record["message"],
                data=data,
            )

            self._store.write(entry)
        except Exception as exc:
            print(f"[nano-alice log] sink error: {exc}", file=sys.stderr)
