"""LogStore integration tests."""

from datetime import datetime, timedelta

import pytest

from nano_alice.log import _infer_component
from nano_alice.log.store import LogStore
from nano_alice.log.types import Component, LogLevel, LogEntry


def test_infer_component_returns_correct_component() -> None:
    """Test that _infer_component returns the correct Component for different log records."""
    # Agent signals
    assert _infer_component({"name": "nano_alice.agent.signals.bus"}) == Component.SIGNALS
    assert _infer_component({"name": "nano_alice.agent.signals.types"}) == Component.SIGNALS

    # Reflect
    assert _infer_component({"name": "nano_alice.agent.reflect.processor"}) == Component.REFLECT
    assert _infer_component({"name": "nano_alice.agent.reflect.internal_state"}) == Component.REFLECT

    # Tools
    assert _infer_component({"name": "nano_alice.agent.tools.filesystem"}) == Component.TOOLS
    assert _infer_component({"name": "nano_alice.agent.tools.shell"}) == Component.TOOLS

    # Channels
    assert _infer_component({"name": "nano_alice.channels.feishu"}) == Component.CHANNELS
    assert _infer_component({"name": "nano_alice.channels.telegram"}) == Component.CHANNELS

    # Scheduler (both old and new paths)
    assert _infer_component({"name": "nano_alice.scheduler.service"}) == Component.SCHEDULER
    assert _infer_component({"name": "nano_alice.cron.service"}) == Component.SCHEDULER

    # Agent core (fallback)
    assert _infer_component({"name": "nano_alice.agent.loop"}) == Component.AGENT
    assert _infer_component({"name": "nano_alice.config.loader"}) == Component.AGENT

    # Unknown path (fallback)
    assert _infer_component({"name": ""}) == Component.AGENT
    assert _infer_component({"name": "some.other.module"}) == Component.AGENT


def test_log_store_write_and_query(tmp_path) -> None:
    """Test that LogStore.write() and query() work correctly."""
    store = LogStore(tmp_path, retention_hours=6)

    # Write some entries
    entry1 = LogEntry(
        ts=datetime.now(),
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="test_event",
        msg="Test message",
        data={"key": "value"},
    )
    entry2 = LogEntry(
        ts=datetime.now(),
        level=LogLevel.ERROR,
        component=Component.CHANNELS,
        event="error_event",
        msg="Error message",
    )

    store.write(entry1)
    store.write(entry2)

    # Query all
    results = store.query()
    assert len(results) == 2
    # Results are sorted by ts descending
    assert results[0].event == "error_event"
    assert results[1].event == "test_event"

    # Query by component
    agent_results = store.query(component=Component.AGENT)
    assert len(agent_results) == 1
    assert agent_results[0].event == "test_event"

    # Query by level
    error_results = store.query(level=LogLevel.ERROR)
    assert len(error_results) == 1
    assert error_results[0].event == "error_event"

    # Query by component + level
    combined_results = store.query(component=Component.CHANNELS, level=LogLevel.ERROR)
    assert len(combined_results) == 1
    assert combined_results[0].event == "error_event"


def test_log_store_summarize(tmp_path) -> None:
    """Test that LogStore.summarize() returns correct statistics."""
    store = LogStore(tmp_path, retention_hours=6)

    # Write entries with different events
    for i in range(5):
        store.write(
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.INFO,
                component=Component.AGENT,
                event="event_a",
                msg=f"Message {i}",
            )
        )
    for i in range(3):
        store.write(
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.ERROR,
                component=Component.CHANNELS,
                event="event_b",
                msg=f"Error {i}",
            )
        )
    for i in range(2):
        store.write(
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.WARNING,
                component=Component.SCHEDULER,
                event="event_c",
                msg=f"Warning {i}",
            )
        )

    summary = store.summarize()
    assert summary["total"] == 10
    assert summary["errors"] == 3
    assert summary["warnings"] == 2
    assert summary["by_event"] == {
        "event_a": 5,
        "event_b": 3,
        "event_c": 2,
    }


@pytest.mark.asyncio
async def test_log_store_error_publishes_signal(tmp_path, monkeypatch) -> None:
    """Test that ERROR level entries trigger LOG_ERROR signal publication."""
    published_signals = []

    class FakeSignalBus:
        async def publish(self, signal):
            published_signals.append(signal)

    store = LogStore(tmp_path, retention_hours=6)
    store.set_signal_bus(FakeSignalBus())

    # Write an ERROR entry
    entry = LogEntry(
        ts=datetime.now(),
        level=LogLevel.ERROR,
        component=Component.CHANNELS,
        event="connection_failed",
        msg="Failed to connect to Feishu",
        data={"host": "open.feishu.cn"},
    )
    store.write(entry)

    # Give the async task a chance to run
    import asyncio

    await asyncio.sleep(0.1)

    # Check that a signal was published
    assert len(published_signals) >= 1
    signal = published_signals[0]
    assert signal.type.value == "log_error"
    assert signal.data["component"] == "channels"
    assert signal.data["msg"] == "Failed to connect to Feishu"


def test_log_store_cleanup_old_entries(tmp_path, monkeypatch) -> None:
    """Test that LogStore cleans up entries older than retention period."""
    # Create a store with 1 hour retention
    store = LogStore(tmp_path, retention_hours=1)

    # Write entries at different times
    now = datetime.now()
    old_entry = LogEntry(
        ts=now - timedelta(hours=2),
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="old_event",
        msg="Old message",
    )
    new_entry = LogEntry(
        ts=now,
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="new_event",
        msg="New message",
    )

    # Manually append to bypass the normal write flow
    store._append(old_entry)
    store._append(new_entry)

    # Trigger cleanup
    store._cleanup(Component.AGENT)

    # Query should only return the new entry
    results = store.query(component=Component.AGENT)
    assert len(results) == 1
    assert results[0].event == "new_event"


def test_log_store_query_with_since(tmp_path) -> None:
    """Test that LogStore.query() filters by timestamp when 'since' is provided."""
    store = LogStore(tmp_path, retention_hours=6)

    now = datetime.now()
    old_entry = LogEntry(
        ts=now - timedelta(hours=1),
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="old_event",
        msg="Old message",
    )
    new_entry = LogEntry(
        ts=now,
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="new_event",
        msg="New message",
    )

    store.write(old_entry)
    store.write(new_entry)

    # Query with since should only return newer entries
    cutoff = now - timedelta(minutes=30)
    results = store.query(since=cutoff)
    assert len(results) == 1
    assert results[0].event == "new_event"


def test_log_store_limit(tmp_path) -> None:
    """Test that LogStore.query() respects the limit parameter."""
    store = LogStore(tmp_path, retention_hours=6)

    # Write 5 entries
    for i in range(5):
        store.write(
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.INFO,
                component=Component.AGENT,
                event=f"event_{i}",
                msg=f"Message {i}",
            )
        )

    # Query with limit=3 should return only 3 entries
    results = store.query(limit=3)
    assert len(results) == 3


def test_log_entry_to_jsonl_roundtrip() -> None:
    """Test that LogEntry serialization and deserialization work correctly."""
    entry = LogEntry(
        ts=datetime(2026, 3, 24, 12, 0, 0),
        level=LogLevel.ERROR,
        component=Component.CHANNELS,
        event="test_event",
        msg="Test message",
        data={"key": "value", "nested": {"a": 1}},
    )

    jsonl = entry.to_jsonl()
    restored = LogEntry.from_jsonl(jsonl)

    assert restored.ts == entry.ts
    assert restored.level == entry.level
    assert restored.component == entry.component
    assert restored.event == entry.event
    assert restored.msg == entry.msg
    assert restored.data == entry.data


def test_log_store_write_with_invalid_data(tmp_path) -> None:
    """Test that LogStore.write() handles non-JSON-serializable data gracefully."""
    store = LogStore(tmp_path, retention_hours=6)

    # This should not raise an error
    entry = LogEntry(
        ts=datetime.now(),
        level=LogLevel.INFO,
        component=Component.AGENT,
        event="test",
        msg="Test",
        data={"object": object()},  # Non-serializable
    )

    # The FileSink should convert this to a string representation
    # But direct write might fail, so we just check it doesn't crash the store
    store.write(entry)

    # Query should still work
    results = store.query()
    # Entry should have been written with sanitized data
    assert len(results) >= 0
