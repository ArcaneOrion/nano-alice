import pytest

from nano_alice.agent.reminder_intent import ReminderIntentStore
from nano_alice.cron.service import CronService
from nano_alice.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_add_job_creates_reminder_intent_when_store_configured(tmp_path) -> None:
    intent_store = ReminderIntentStore(tmp_path / "workspace")
    service = CronService(tmp_path / "cron" / "jobs.json", intent_store=intent_store)

    job = service.add_job(
        name="drink water",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="Drink water",
        channel="feishu",
        to="chat1",
    )

    assert job.payload.intent_id
    intent = intent_store.load(job.payload.intent_id or "")
    assert intent is not None
    assert intent.session_key == "feishu:chat1"
    assert intent.goal == "Drink water"
