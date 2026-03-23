"""Scheduler types.

Renamed from cron.types.py - data structures for scheduled jobs.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Schedule:
    """Schedule definition for a job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class JobPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "system_event"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number


@dataclass
class JobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class ScheduledJob:
    """A scheduled job (formerly CronJob)."""
    id: str
    name: str
    enabled: bool = True
    schedule: Schedule = field(default_factory=lambda: Schedule(kind="every"))
    payload: JobPayload = field(default_factory=JobPayload)
    state: JobState = field(default_factory=JobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class SchedulerStore:
    """Persistent store for scheduled jobs (formerly CronStore)."""
    version: int = 1
    jobs: list[ScheduledJob] = field(default_factory=list)


# Backward compatibility aliases for transition period
CronSchedule = Schedule
CronPayload = JobPayload
CronJobState = JobState
CronJob = ScheduledJob
CronStore = SchedulerStore
