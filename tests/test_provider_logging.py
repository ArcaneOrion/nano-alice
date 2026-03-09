import asyncio

from loguru import logger

from nano_alice.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    build_provider_metadata,
    normalize_endpoint_label,
    preview_text,
    sanitize_headers,
    summarize_tool_calls,
)
from nano_alice.providers.rotating_provider import RotatingProvider


class _StubProvider(LLMProvider):
    def __init__(self, name: str, responses: list[LLMResponse], api_base: str | None = None):
        super().__init__(api_key=None, api_base=api_base)
        self.name = name
        self.responses = list(responses)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content=f"{self.name}-ok")

    def get_default_model(self) -> str:
        return self.name


def _capture_logs(level: str = "DEBUG") -> tuple[int, list[str]]:
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(str(msg)), level=level)
    return sink_id, messages


def test_preview_text_collapses_whitespace_and_truncates() -> None:
    assert preview_text("  hello\n   world  ") == "hello world"
    assert preview_text("x" * 10, limit=5) == "xxxxx..."


def test_normalize_endpoint_label_strips_query_and_fragment() -> None:
    endpoint = normalize_endpoint_label("https://api.example.com/v1/chat?token=secret#frag")

    assert endpoint == "https://api.example.com/v1/chat"


def test_sanitize_headers_redacts_sensitive_values() -> None:
    headers = sanitize_headers({"Authorization": "Bearer secret", "X-Trace": "abc"})

    assert headers == {"Authorization": "***", "X-Trace": "abc"}


def test_summarize_tool_calls_reports_name_and_argument_count() -> None:
    summary = summarize_tool_calls(
        [
            ToolCallRequest(id="1", name="search", arguments={"q": "hi"}),
            ToolCallRequest(id="2", name="open", arguments={"url": "u", "mode": "r"}),
        ]
    )

    assert summary == "search(1), open(2)"


def test_build_provider_metadata_normalizes_endpoint() -> None:
    metadata = build_provider_metadata(
        provider_name="LiteLLMProvider",
        requested_model="openai/gpt-5",
        resolved_model="openai/gpt-5",
        endpoint="https://api.example.com/v1?token=secret",
    )

    assert metadata == {
        "provider_name": "LiteLLMProvider",
        "requested_model": "openai/gpt-5",
        "resolved_model": "openai/gpt-5",
        "endpoint": "https://api.example.com/v1",
    }


def test_rotating_provider_logs_failover_and_recovery() -> None:
    first = _StubProvider(
        "openai1/gpt-5.4",
        [LLMResponse(content="Error: rate limited", finish_reason="error"), LLMResponse(content="primary ok")],
        api_base="https://route-1.example.com/v1",
    )
    second = _StubProvider(
        "openai2/gpt-5.4",
        [LLMResponse(content="fallback ok")],
        api_base="https://route-2.example.com/v1",
    )
    now = {"value": 0.0}
    provider = RotatingProvider(first, [second], cooldown_seconds=30, time_fn=lambda: now["value"])

    sink_id, messages = _capture_logs()
    try:
        response1 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}]))
        now["value"] = 31.0
        response2 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "again"}]))
    finally:
        logger.remove(sink_id)

    combined = "\n".join(messages)
    assert response1.content == "fallback ok"
    assert response2.content == "primary ok"
    assert "Primary endpoint failed, switching to fallback pool" in combined
    assert "Endpoint pool attempt: fallback_index=0" in combined
    assert "Endpoint pool locked on fallback: index=0" in combined
    assert "Endpoint pool retrying primary after cooldown" in combined
    assert "https://route-1.example.com/v1" in combined
    assert "https://route-2.example.com/v1" in combined
