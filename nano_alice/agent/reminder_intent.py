"""Persistent reminder intent state for internal agent notifications."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nano_alice.utils.helpers import ensure_dir, safe_filename

DeliveryStatus = Literal["pending", "sent", "failed", "skipped"]


@dataclass
class DeliveryState:
    """Last known delivery state for a reminder intent."""

    status: DeliveryStatus = "pending"
    last_message_id: str = ""
    last_error: str = ""
    last_delivery_at: str = ""


@dataclass
class ReminderIntent:
    """Long-lived internal reminder intent."""

    intent_id: str
    session_key: str
    origin_channel: str
    origin_chat_id: str
    goal: str
    why_notify: str
    notify_policy: dict[str, Any] = field(default_factory=dict)
    last_notified_at: str = ""
    delivery_state: DeliveryState = field(default_factory=DeliveryState)
    active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReminderIntent":
        delivery = DeliveryState(**(data.get("delivery_state") or {}))
        return cls(
            intent_id=data["intent_id"],
            session_key=data["session_key"],
            origin_channel=data["origin_channel"],
            origin_chat_id=data["origin_chat_id"],
            goal=data.get("goal", ""),
            why_notify=data.get("why_notify", ""),
            notify_policy=data.get("notify_policy") or {},
            last_notified_at=data.get("last_notified_at", ""),
            delivery_state=delivery,
            active=data.get("active", True),
            created_at=data.get("created_at", datetime.now().isoformat(timespec="seconds")),
            updated_at=data.get("updated_at", datetime.now().isoformat(timespec="seconds")),
        )


class ReminderIntentStore:
    """Persist reminder intents under the workspace."""

    def __init__(self, workspace: Path):
        self.root = ensure_dir(workspace / "intents")

    def _path(self, intent_id: str) -> Path:
        return self.root / f"{safe_filename(intent_id)}.json"

    def create(
        self,
        *,
        session_key: str,
        origin_channel: str,
        origin_chat_id: str,
        goal: str,
        why_notify: str,
        notify_policy: dict[str, Any] | None = None,
        intent_id: str | None = None,
    ) -> ReminderIntent:
        intent = ReminderIntent(
            intent_id=intent_id or f"intent_{uuid.uuid4().hex[:10]}",
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            goal=goal.strip(),
            why_notify=why_notify.strip(),
            notify_policy=notify_policy or {
                "allow_push": True,
                "retry_on_failure": False,
                "allow_repeat": True,
            },
        )
        self.save(intent)
        return intent

    def load(self, intent_id: str) -> ReminderIntent | None:
        path = self._path(intent_id)
        if not path.exists():
            return None
        try:
            return ReminderIntent.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(self, intent: ReminderIntent) -> None:
        intent.updated_at = datetime.now().isoformat(timespec="seconds")
        self._path(intent.intent_id).write_text(
            json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure(
        self,
        *,
        session_key: str,
        origin_channel: str,
        origin_chat_id: str,
        goal: str,
        why_notify: str,
        notify_policy: dict[str, Any] | None = None,
        intent_id: str | None = None,
    ) -> ReminderIntent:
        if intent_id:
            existing = self.load(intent_id)
            if existing is not None:
                return existing
        return self.create(
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            goal=goal,
            why_notify=why_notify,
            notify_policy=notify_policy,
            intent_id=intent_id,
        )

    def mark_notified(self, intent_id: str, when: str | None = None) -> ReminderIntent | None:
        intent = self.load(intent_id)
        if intent is None:
            return None
        intent.last_notified_at = when or datetime.now().isoformat(timespec="seconds")
        self.save(intent)
        return intent

    def update_delivery(
        self,
        intent_id: str,
        *,
        status: DeliveryStatus,
        message_id: str = "",
        error: str = "",
        delivered_at: str | None = None,
    ) -> ReminderIntent | None:
        intent = self.load(intent_id)
        if intent is None:
            return None
        intent.delivery_state.status = status
        intent.delivery_state.last_message_id = message_id
        intent.delivery_state.last_error = error
        intent.delivery_state.last_delivery_at = delivered_at or datetime.now().isoformat(
            timespec="seconds"
        )
        self.save(intent)
        return intent
