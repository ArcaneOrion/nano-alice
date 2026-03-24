"""Tests for the signal system."""

import asyncio
from datetime import datetime, timedelta

import pytest

from nano_alice.agent.reflect.internal_state import InternalState
from nano_alice.agent.signals.bus import SignalBus
from nano_alice.agent.signals.types import AgentSignal, Signal
from nano_alice.log.store import LogStore
from nano_alice.log.types import Component, LogEntry, LogLevel


class TestSignalBus:
    """Test the SignalBus publish-subscribe system."""

    @pytest.fixture
    def signal_bus(self):
        """Create a fresh signal bus for each test."""
        bus = SignalBus()
        asyncio.run(bus.start())
        yield bus
        bus.stop()

    @pytest.mark.asyncio
    async def test_subscribe_publish(self, signal_bus):
        """Test basic subscribe and publish."""
        received = []

        async def handler(signal: Signal):
            received.append(signal)

        signal_bus.subscribe(AgentSignal.STARTUP, handler)
        await signal_bus.publish(Signal.startup())

        # Wait a bit for async handler to complete
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].type == AgentSignal.STARTUP

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, signal_bus):
        """Test multiple subscribers to the same signal."""
        received_1 = []
        received_2 = []

        async def handler_1(signal: Signal):
            received_1.append(signal)

        async def handler_2(signal: Signal):
            received_2.append(signal)

        signal_bus.subscribe(AgentSignal.TODO_CHECK, handler_1)
        signal_bus.subscribe(AgentSignal.TODO_CHECK, handler_2)
        await signal_bus.publish(Signal.todo_check())

        await asyncio.sleep(0.1)

        assert len(received_1) == 1
        assert len(received_2) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self, signal_bus):
        """Test unsubscribe functionality."""
        received = []

        async def handler(signal: Signal):
            received.append(signal)

        signal_bus.subscribe(AgentSignal.SCHEDULE_TRIGGER, handler)
        signal_bus.unsubscribe(AgentSignal.SCHEDULE_TRIGGER, handler)
        # Create signal without job (for testing)
        await signal_bus.publish(Signal.schedule_trigger())

        # Wait a bit for async handler to complete
        await asyncio.sleep(0.1)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_signal_with_data(self, signal_bus):
        """Test signal data passing."""
        received = []

        async def handler(signal: Signal):
            received.append(signal)

        signal_bus.subscribe(AgentSignal.SCHEDULE_TRIGGER, handler)
        # Create signal with data
        signal = Signal(
            type=AgentSignal.SCHEDULE_TRIGGER,
            data={"job_id": "123", "message": "test message"}
        )
        await signal_bus.publish(signal)

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].data["job_id"] == "123"

    def test_is_running(self, signal_bus):
        """Test is_running property."""
        assert signal_bus.is_running is True
        signal_bus.stop()
        assert signal_bus.is_running is False


    def test_schedule_trigger_carries_payload_kind(self):
        """Test scheduled signals preserve payload kind semantics."""
        from nano_alice.scheduler.types import JobPayload, JobState, Schedule, ScheduledJob

        job = ScheduledJob(
            id="job-1",
            name="water reminder",
            schedule=Schedule(kind="at", at_ms=1234567890),
            payload=JobPayload(kind="system_event", message="remind me", deliver=True),
            state=JobState(),
        )

        signal = Signal.schedule_trigger(job)

        assert signal.type == AgentSignal.SCHEDULE_TRIGGER
        assert signal.data["payload_kind"] == "system_event"
        assert signal.data["message"] == "remind me"


@pytest.mark.asyncio
async def test_log_store_publishes_log_error_signal(tmp_path):
    signal_bus = SignalBus()
    await signal_bus.start()
    received = []

    async def handler(signal: Signal):
        received.append(signal)

    signal_bus.subscribe(AgentSignal.LOG_ERROR, handler)

    store = LogStore(tmp_path)
    store.set_signal_bus(signal_bus)
    entry = LogEntry(
        ts=datetime.now(),
        level=LogLevel.ERROR,
        component=Component.REFLECT,
        event="test_error",
        msg="boom",
    )

    store.write(entry)
    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].type == AgentSignal.LOG_ERROR
    assert received[0].data == {
        "component": "reflect",
        "msg": "boom",
        "ts": entry.ts.isoformat(),
    }

    signal_bus.stop()


def test_internal_state_error_count_tracks_last_hour_window():
    state = InternalState()
    now = datetime.now()

    state.record_error("scheduler", "old error", (now - timedelta(hours=2)).isoformat())
    assert state.error_count_last_hour == 0
    assert state.get_health_status() == "healthy"

    for i in range(6):
        state.record_error("scheduler", f"recent {i}", (now - timedelta(minutes=10 - i)).isoformat())

    assert state.error_count_last_hour == 6
    assert state.get_health_status() == "degraded"

    later = now + timedelta(hours=2)
    assert state.get_health_status(now=later) == "healthy"
    assert state.error_count_last_hour == 0
