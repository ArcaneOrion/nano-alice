import json

import pytest

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


def test_add_job_defaults_to_system_event(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="reminder",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="drink water",
    )

    assert job.payload.kind == "system_event"


def test_add_job_preserves_explicit_agent_turn_kind(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="chat turn",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="follow up",
        payload_kind="agent_turn",
    )

    assert job.payload.kind == "agent_turn"


def test_persistence_preserves_payload_kind(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service1 = CronService(store_path)
    job_id = service1.add_job(
        name="persistent",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="saved",
        payload_kind="agent_turn",
    ).id

    service2 = CronService(store_path)
    jobs = service2.list_jobs(include_disabled=True)

    assert len(jobs) == 1
    assert jobs[0].id == job_id
    assert jobs[0].payload.kind == "agent_turn"


def test_load_store_defaults_missing_payload_kind_to_system_event(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy01",
                        "name": "legacy reminder",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 1000},
                        "payload": {
                            "message": "drink water",
                            "deliver": False,
                            "channel": "telegram",
                            "to": "user-1",
                        },
                        "state": {},
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    jobs = service.list_jobs(include_disabled=True)

    assert len(jobs) == 1
    assert jobs[0].payload.kind == "system_event"
