"""Cron service for scheduled agent tasks.

DEPRECATED: This module is renamed to 'scheduler'. Import from scheduler instead.
This file remains for backward compatibility during the transition period.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

# Import the new scheduler service as base
from nano_alice.scheduler.service import SchedulerService
from nano_alice.scheduler.types import (
    Schedule,
    ScheduledJob,
    SchedulerStore,
    JobPayload,
    JobState,
)

# Backward compatibility aliases
CronSchedule = Schedule
CronJob = ScheduledJob
CronStore = SchedulerStore


class CronService(SchedulerService):
    """
    Legacy-compatible CronService that wraps the new SchedulerService.

    Supports the old on_job callback pattern for backward compatibility.
    When on_job is set, jobs are executed via callback instead of SignalBus.
    """

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
    ):
        # Initialize parent without signal_bus (we use callback instead)
        super().__init__(store_path, signal_bus=None)
        self._legacy_on_job = on_job

    @property
    def on_job(self) -> Callable[[CronJob], Coroutine[Any, Any, str | None]] | None:
        """Get the legacy callback."""
        return self._legacy_on_job

    @on_job.setter
    def on_job(self, value: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None) -> None:
        """Set the legacy callback."""
        self._legacy_on_job = value

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a job using the legacy callback if available."""
        from loguru import logger

        start_ms = _now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            response = None
            if self._legacy_on_job:
                response = await self._legacy_on_job(job)

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("Cron: job '{}' failed: {}", job.name, e)

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                if self._store:
                    self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # Compute next run
            if self._store:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter
            from datetime import datetime
            from zoneinfo import ZoneInfo
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


__all__ = ["CronService", "CronJob", "CronSchedule", "CronStore"]
