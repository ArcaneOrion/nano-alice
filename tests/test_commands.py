import shutil
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nano_alice.cli.commands import app, _make_provider
from nano_alice.config.schema import Config
from nano_alice.providers.base import LLMProvider, LLMResponse
from nano_alice.providers.custom_provider import CustomProvider
from nano_alice.providers.litellm_provider import LiteLLMProvider
from nano_alice.providers.openai_codex_provider import OpenAICodexProvider, _strip_model_prefix
from nano_alice.providers.rotating_provider import RotatingProvider
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
    assert (workspace_dir / "IDENTITY.md").exists()
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
    assert _strip_model_prefix("openaiCodex/gpt-5.4") == "gpt-5.4"


def test_make_provider_uses_responses_provider_for_openai_codex_when_api_key_configured():
    config = Config()
    config.agents.defaults.model = "openaiCodex/gpt-5.4"
    config.providers.openai_codex.api_key = "sk-test"
    config.providers.openai_codex.api_base = "https://example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, OpenAICodexProvider)
    assert provider.default_model == "openaiCodex/gpt-5.4"
    assert provider.api_key == "sk-test"
    assert provider.api_base == "https://example.com/v1"


def test_make_provider_uses_custom_provider_for_openai_with_custom_api_base():
    config = Config()
    config.agents.defaults.model = "openai/gpt-5.4"
    config.providers.openai.api_key = "sk-test"
    config.providers.openai.api_base = "https://example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, CustomProvider)
    assert provider.default_model == "gpt-5.4"
    assert provider.api_key == "sk-test"
    assert provider.api_base == "https://example.com/v1"


def test_explicit_provider_name_overrides_model_keyword_matching():
    config = Config()
    config.agents.defaults.provider = "openai"
    config.agents.defaults.model = "gpt-5.1-codex"
    config.providers.openai.api_key = "sk-openai"
    config.providers.openai_codex.api_key = "sk-codex"
    config.providers.openai_codex.api_base = "https://example.com/v1"

    assert config.get_provider_name() == "openai"


def test_make_provider_uses_explicit_openai_codex_route():
    config = Config()
    config.agents.defaults.provider = "openaiCodex"
    config.agents.defaults.model = "gpt-5.4"
    config.providers.openai_codex.api_key = "sk-test"
    config.providers.openai_codex.api_base = "https://example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, OpenAICodexProvider)
    assert provider.default_model == "gpt-5.4"
    assert provider.api_key == "sk-test"
    assert provider.api_base == "https://example.com/v1"


def test_config_matches_explicit_openai_route_prefix():
    config = Config()
    config.agents.defaults.model = "openai2/gpt-5.4"
    config.providers.openai_2.api_key = "sk-route-2"
    config.providers.openai_2.api_base = "https://route-2.example.com/v1"

    assert config.get_provider_name() == "openai_2"
    assert config.get_api_base() == "https://route-2.example.com/v1"


def test_make_provider_uses_custom_provider_for_explicit_openai_route():
    config = Config()
    config.agents.defaults.model = "openai1/gpt-5.4"
    config.providers.openai_1.api_key = "sk-route-1"
    config.providers.openai_1.api_base = "https://route-1.example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, CustomProvider)
    assert provider.default_model == "gpt-5.4"
    assert provider.api_key == "sk-route-1"
    assert provider.api_base == "https://route-1.example.com/v1"


def test_make_provider_uses_rotating_provider_for_default_models():
    config = Config()
    config.agents.defaults.model = "openai1/gpt-5.4"
    config.agents.defaults.models = ["openai1/gpt-5.4", "openai2/gpt-5.4", "openai3/gpt-5.4"]
    config.providers.openai_1.api_key = "sk-route-1"
    config.providers.openai_1.api_base = "https://route-1.example.com/v1"
    config.providers.openai_2.api_key = "sk-route-2"
    config.providers.openai_2.api_base = "https://route-2.example.com/v1"
    config.providers.openai_3.api_key = "sk-route-3"
    config.providers.openai_3.api_base = "https://route-3.example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, RotatingProvider)
    assert provider.primary_provider.api_base == "https://route-1.example.com/v1"
    assert [child.api_base for child in provider.fallback_providers] == [
        "https://route-2.example.com/v1",
        "https://route-3.example.com/v1",
    ]


def test_make_provider_uses_first_fallback_as_primary_when_model_not_explicitly_set():
    config = Config()
    config.agents.defaults.models = ["openai1/gpt-5.4", "openai2/gpt-5.4"]
    config.providers.openai_1.api_key = "sk-route-1"
    config.providers.openai_1.api_base = "https://route-1.example.com/v1"
    config.providers.openai_2.api_key = "sk-route-2"
    config.providers.openai_2.api_base = "https://route-2.example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, RotatingProvider)
    assert provider.primary_provider.api_base == "https://route-1.example.com/v1"
    assert [child.api_base for child in provider.fallback_providers] == ["https://route-2.example.com/v1"]


class _StubProvider(LLMProvider):
    def __init__(self, name: str, responses: list[LLMResponse]):
        super().__init__()
        self.name = name
        self.responses = list(responses)
        self.calls = 0
        self.received_models: list[str | None] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        self.received_models.append(model)
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content=f"{self.name}-ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return self.name


def test_rotating_provider_fails_over_to_fallback_and_sticks_during_cooldown():
    first = _StubProvider(
        "openai1/gpt-5.4",
        [LLMResponse(content="Error: rate limited", finish_reason="error")],
    )
    second = _StubProvider("openai2/gpt-5.4", [LLMResponse(content="second-ok"), LLMResponse(content="second-still-ok")])
    provider = RotatingProvider(first, [second], cooldown_seconds=900, time_fn=lambda: 0.0)

    response1 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}], model="anthropic/claude-opus-4-5-20251101"))
    response2 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "again"}], model="anthropic/claude-opus-4-5-20251101"))

    assert response1.content == "second-ok"
    assert response2.content == "second-still-ok"
    assert first.calls == 1
    assert second.calls == 2
    assert first.received_models == ["anthropic/claude-opus-4-5-20251101"]
    assert second.received_models == [None, None]


def test_rotating_provider_retries_primary_after_cooldown():
    now = {"value": 0.0}
    first = _StubProvider(
        "openai1/gpt-5.4",
        [LLMResponse(content="Error: rate limited", finish_reason="error"), LLMResponse(content="primary-recovered")],
    )
    second = _StubProvider("openai2/gpt-5.4", [LLMResponse(content="fallback-ok")])
    provider = RotatingProvider(first, [second], cooldown_seconds=900, time_fn=lambda: now["value"])

    response1 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}]))
    now["value"] = 901.0
    response2 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "retry"}]))

    assert response1.content == "fallback-ok"
    assert response2.content == "primary-recovered"
    assert first.calls == 2
    assert second.calls == 1


def test_rotating_provider_deduplicates_primary_from_fallback_pool():
    config = Config()
    config.agents.defaults.model = "openai1/gpt-5.4"
    config.agents.defaults.models = ["openai-1/gpt-5.4", "openai2/gpt-5.4", "openai2/gpt-5.4"]
    config.providers.openai_1.api_key = "sk-route-1"
    config.providers.openai_1.api_base = "https://route-1.example.com/v1"
    config.providers.openai_2.api_key = "sk-route-2"
    config.providers.openai_2.api_base = "https://route-2.example.com/v1"

    provider = _make_provider(config)

    assert isinstance(provider, RotatingProvider)
    assert provider.primary_provider.api_base == "https://route-1.example.com/v1"
    assert [child.api_base for child in provider.fallback_providers] == ["https://route-2.example.com/v1"]
