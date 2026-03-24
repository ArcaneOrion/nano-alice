"""Shared fixtures for nano-alice tests."""

from pathlib import Path

import pytest

from nano_alice.bus.queue import MessageBus


@pytest.fixture
def message_bus() -> MessageBus:
    """Real MessageBus for integration tests."""
    return MessageBus()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Temporary workspace directory structure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "skills").mkdir()
    (workspace / "memory").mkdir()
    return workspace
