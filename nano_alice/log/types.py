"""Log entry types for structured logging."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Component(Enum):
    AGENT = "agent"
    CHANNELS = "channels"
    SCHEDULER = "scheduler"
    SIGNALS = "signals"
    REFLECT = "reflect"
    TOOLS = "tools"


@dataclass
class LogEntry:
    ts: datetime
    level: LogLevel
    component: Component
    event: str
    msg: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "ts": self.ts.isoformat(),
                "level": self.level.value,
                "component": self.component.value,
                "event": self.event,
                "msg": self.msg,
                "data": self.data,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_jsonl(cls, line: str) -> LogEntry:
        d = json.loads(line)
        return cls(
            ts=datetime.fromisoformat(d["ts"]),
            level=LogLevel(d["level"]),
            component=Component(d["component"]),
            event=d["event"],
            msg=d["msg"],
            data=d.get("data", {}),
        )
