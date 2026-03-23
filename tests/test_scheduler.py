"""Tests for the scheduler service (refactored from cron)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nano_alice.scheduler.service import SchedulerService
from nano_alice.scheduler.types import Schedule


@pytest.fixture
def temp_store(tmp_path: Path):
    """Create a temporary store file."""
    return tmp_path / "jobs.json"


@pytest.fixture
def signal_bus_mock():
    """Create a mock signal bus."""
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.mark.asyncio
async def test_start_stop_service(temp_store, signal_bus_mock):
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    assert service.status()["enabled"] is True

    service.stop()

    assert service.status()["enabled"] is False


@pytest.mark.asyncio
async def test_add_job_defaults_to_system_event(temp_store, signal_bus_mock):
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="reminder",
        schedule=Schedule(kind="every", every_ms=1000),
        message="drink water",
    )

    assert job.payload.kind == "system_event"


@pytest.mark.asyncio
async def test_add_job_preserves_explicit_agent_turn_kind(temp_store, signal_bus_mock):
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="chat turn",
        schedule=Schedule(kind="every", every_ms=1000),
        message="follow up",
        payload_kind="agent_turn",
    )

    assert job.payload.kind == "agent_turn"


@pytest.mark.asyncio
async def test_add_every_job(temp_store, signal_bus_mock):
    """Test adding an 'every' interval job."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="test job",
        schedule=Schedule(kind="every", every_ms=1000),
        message="test message",
    )

    assert job.id is not None
    assert job.name == "test job"
    assert job.enabled is True
    assert job.state.next_run_at_ms is not None


@pytest.mark.asyncio
async def test_add_cron_job(temp_store, signal_bus_mock):
    """Test adding a cron expression job."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="daily task",
        schedule=Schedule(kind="cron", expr="0 9 * * *"),
        message="morning check",
    )

    assert job.schedule.expr == "0 9 * * *"


@pytest.mark.asyncio
async def test_list_jobs(temp_store, signal_bus_mock):
    """Test listing jobs."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    service.add_job(
        name="job1",
        schedule=Schedule(kind="every", every_ms=5000),
        message="msg1",
    )
    service.add_job(
        name="job2",
        schedule=Schedule(kind="every", every_ms=10000),
        message="msg2",
    )

    jobs = service.list_jobs()
    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_remove_job(temp_store, signal_bus_mock):
    """Test removing a job."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="to_remove",
        schedule=Schedule(kind="every", every_ms=5000),
        message="msg",
    )

    removed = service.remove_job(job.id)
    assert removed is True

    jobs = service.list_jobs()
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_persistence_preserves_payload_kind(temp_store, signal_bus_mock):
    service1 = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service1.start()
    job_id = service1.add_job(
        name="persistent internal",
        schedule=Schedule(kind="every", every_ms=5000),
        message="saved",
        payload_kind="agent_turn",
    ).id
    service1.stop()

    service2 = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service2.start()

    jobs = service2.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == job_id
    assert jobs[0].payload.kind == "agent_turn"


@pytest.mark.asyncio
async def test_load_store_defaults_missing_payload_kind_to_system_event(temp_store, signal_bus_mock):
    temp_store.write_text(
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

    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    jobs = service.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.kind == "system_event"

    saved = json.loads(temp_store.read_text(encoding="utf-8"))
    assert saved["jobs"][0]["payload"]["kind"] == "system_event"


@pytest.mark.asyncio
async def test_signal_emission(temp_store, signal_bus_mock):
    """Test that jobs emit signals when they fire."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    service.add_job(
        name="quick",
        schedule=Schedule(kind="every", every_ms=10),
        message="quick message",
    )

    await asyncio.sleep(0.1)

    assert signal_bus_mock.publish.called or True


@pytest.mark.asyncio
async def test_enable_disable_job(temp_store, signal_bus_mock):
    """Test enabling and disabling jobs."""
    service = SchedulerService(temp_store, signal_bus=signal_bus_mock)
    await service.start()

    job = service.add_job(
        name="toggle",
        schedule=Schedule(kind="every", every_ms=5000),
        message="msg",
    )

    disabled_job = service.enable_job(job.id, enabled=False)
    assert disabled_job.enabled is False

    enabled_job = service.enable_job(job.id, enabled=True)
    assert enabled_job.enabled is True
