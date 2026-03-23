import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from nano_alice.agent.signals.types import AgentSignal
from nano_alice.cli.commands import app
from nano_alice.config.schema import Config
from nano_alice.providers.litellm_provider import LiteLLMProvider
from nano_alice.providers.openai_codex_provider import _strip_model_prefix
from nano_alice.providers.registry import find_by_model

runner = CliRunner()


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with patch("nano_alice.config.loader.get_config_path") as mock_cp, \
         patch("nano_alice.config.loader.save_config") as mock_sc, \
         patch("nano_alice.config.loader.load_config") as mock_lc, \
         patch("nano_alice.utils.helpers.get_workspace_path") as mock_ws:

        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_sc.side_effect = lambda config: config_file.write_text("{}")

        yield config_file, workspace_dir

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "nano-alice is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"


def test_litellm_provider_canonicalizes_github_copilot_hyphen_prefix():
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")

    resolved = provider._resolve_model("github-copilot/gpt-5.3-codex")

    assert resolved == "github_copilot/gpt-5.3-codex"


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


def test_gateway_wires_log_signal_bus_and_reflect_subscriptions(monkeypatch, tmp_path):
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")

    log_store = SimpleNamespace(set_signal_bus=Mock())
    signal_bus = SimpleNamespace(subscribe=Mock())
    scheduler = SimpleNamespace(status=lambda: {"jobs": 0})
    todo = SimpleNamespace()
    bus = SimpleNamespace()
    provider = object()
    session_manager = object()
    channels = SimpleNamespace(enabled_channels=[], stop_all=AsyncMock(), start_all=AsyncMock())
    reflect_process = AsyncMock()
    agent = SimpleNamespace(
        reflect_processor=SimpleNamespace(process=reflect_process),
        run=AsyncMock(),
        close_mcp=AsyncMock(),
        stop=lambda: None,
    )

    monkeypatch.setattr("nano_alice.log.ensure_logging_initialized", lambda: log_store)
    monkeypatch.setattr("nano_alice.log.set_console_level", lambda level: None)
    monkeypatch.setattr("nano_alice.config.loader.load_config", lambda: config)
    monkeypatch.setattr("nano_alice.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("nano_alice.cli.commands._make_provider", lambda cfg: provider)
    monkeypatch.setattr("nano_alice.bus.queue.MessageBus", lambda: bus)
    monkeypatch.setattr("nano_alice.session.manager.SessionManager", lambda workspace: session_manager)
    monkeypatch.setattr("nano_alice.agent.signals.bus.SignalBus", lambda: signal_bus)
    monkeypatch.setattr("nano_alice.scheduler.service.SchedulerService", lambda store_path, signal_bus: scheduler)
    monkeypatch.setattr("nano_alice.agent.loop.AgentLoop", lambda **kwargs: agent)
    monkeypatch.setattr(
        "nano_alice.todo.service.TODOService",
        lambda **kwargs: todo,
    )
    monkeypatch.setattr("nano_alice.channels.manager.ChannelManager", lambda cfg, msg_bus: channels)
    monkeypatch.setattr("asyncio.run", lambda coro: coro.close())

    result = runner.invoke(app, ["gateway"])

    assert result.exit_code == 0
    log_store.set_signal_bus.assert_called_once_with(signal_bus)

    subscribed = signal_bus.subscribe.call_args_list
    assert subscribed == [
        ((AgentSignal.SCHEDULE_TRIGGER, reflect_process),),
        ((AgentSignal.TODO_CHECK, reflect_process),),
        ((AgentSignal.LOG_ERROR, reflect_process),),
    ]
