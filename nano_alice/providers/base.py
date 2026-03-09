"""Base LLM provider interface."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import json_repair


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass


def preview_text(text: str | None, limit: int = 200) -> str:
    """Return a single-line preview for logging."""
    if text is None:
        return ""

    preview = " ".join(text.strip().split())
    if len(preview) <= limit:
        return preview
    return preview[:limit] + "..."


def normalize_endpoint_label(endpoint: str | None) -> str:
    """Return a stable endpoint label without query params or fragments."""
    if not endpoint:
        return "-"

    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return endpoint

    netloc = parts.hostname or parts.netloc or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    sanitized = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return sanitized or endpoint


def summarize_tool_calls(tool_calls: list[ToolCallRequest]) -> str:
    """Return a concise tool-call summary."""
    if not tool_calls:
        return "-"
    parts: list[str] = []
    for tool_call in tool_calls:
        arg_count = len(tool_call.arguments) if isinstance(tool_call.arguments, dict) else 0
        parts.append(f"{tool_call.name}({arg_count})")
    return ", ".join(parts)


def sanitize_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Redact sensitive headers for logs."""
    if not headers:
        return {}

    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"authorization", "proxy-authorization", "x-api-key", "api-key"}:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def build_provider_metadata(
    *,
    provider_name: str,
    requested_model: str | None,
    resolved_model: str | None = None,
    endpoint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create standard provider-call metadata for logs and tracing."""
    metadata: dict[str, Any] = {
        "provider_name": provider_name,
        "requested_model": requested_model or "-",
        "resolved_model": resolved_model or requested_model or "-",
        "endpoint": normalize_endpoint_label(endpoint),
    }
    if extra:
        metadata.update(extra)
    return metadata


def parse_llm_response_payload(response: Any) -> LLMResponse:
    """Parse multiple OpenAI-compatible response shapes into ``LLMResponse``.

    Supports standard Chat Completions objects/dicts, Responses API-style payloads,
    JSON strings returned by some proxy layers, and plain-text fallback responses.
    """
    normalized = _normalize_payload(response)

    if isinstance(normalized, str):
        text = normalized.strip()
        if text.startswith("<!DOCTYPE html") or text.startswith("<html"):
            return LLMResponse(content=f"Error: received HTML response: {text[:200]}", finish_reason="error")
        return LLMResponse(content=normalized, finish_reason="stop")

    if isinstance(normalized, dict):
        error_message = _extract_error_message(normalized)
        if error_message:
            return LLMResponse(content=f"Error: {error_message}", finish_reason="error")

        choices = normalized.get("choices")
        if isinstance(choices, list) and choices:
            return _parse_choices_payload(normalized, choices)

        responses_api = _parse_responses_payload(normalized)
        if responses_api is not None:
            return responses_api

        text = _extract_text_candidate(normalized)
        if text is not None:
            return LLMResponse(content=text, finish_reason=str(normalized.get("finish_reason") or "stop"))

    return LLMResponse(
        content=f"Error: unsupported LLM response format ({type(response).__name__})",
        finish_reason="error",
    )


def _normalize_payload(response: Any) -> Any:
    if isinstance(response, str):
        text = response.strip()
        if not text:
            return ""
        if text[0] not in '{["':
            return response
        try:
            return json_repair.loads(text)
        except Exception:
            try:
                return json.loads(text)
            except Exception:
                return response

    if isinstance(response, dict):
        return response

    for method_name in ("model_dump", "dict"):
        method = getattr(response, method_name, None)
        if callable(method):
            try:
                payload = method()
            except TypeError:
                payload = method(exclude_none=False)
            if isinstance(payload, dict):
                return payload

    if hasattr(response, "__dict__") and isinstance(response.__dict__, dict):
        return response.__dict__

    return response


def _parse_choices_payload(payload: dict[str, Any], choices: list[Any]) -> LLMResponse:
    choice = _normalize_payload(choices[0])
    if not isinstance(choice, dict):
        return LLMResponse(content=f"Error: unsupported choice format ({type(choice).__name__})", finish_reason="error")

    message = _normalize_payload(choice.get("message") or {})
    if not isinstance(message, dict):
        message = {}

    return LLMResponse(
        content=_extract_text_candidate(message),
        tool_calls=_extract_tool_calls(message.get("tool_calls") or []),
        finish_reason=str(choice.get("finish_reason") or payload.get("finish_reason") or "stop"),
        usage=_extract_usage(payload.get("usage")),
        reasoning_content=_extract_reasoning_content(message),
    )


def _parse_responses_payload(payload: dict[str, Any]) -> LLMResponse | None:
    output = payload.get("output")
    if not isinstance(output, list) and "output_text" not in payload:
        return None

    collected_text: list[str] = []
    tool_calls: list[ToolCallRequest] = []

    top_level_output_text = payload.get("output_text")
    if isinstance(top_level_output_text, str) and top_level_output_text:
        collected_text.append(top_level_output_text)

    if isinstance(output, list):
        for item in output:
            normalized_item = _normalize_payload(item)
            if not isinstance(normalized_item, dict):
                continue

            item_type = normalized_item.get("type")
            if item_type == "function_call":
                tool_calls.extend(_extract_tool_calls([normalized_item]))

            content = normalized_item.get("content")
            if isinstance(content, list):
                for block in content:
                    normalized_block = _normalize_payload(block)
                    if not isinstance(normalized_block, dict):
                        continue
                    if normalized_block.get("type") in {"output_text", "text"}:
                        text = normalized_block.get("text")
                        if isinstance(text, str) and text:
                            collected_text.append(text)

            if item_type in {"message", "output_text"} and not isinstance(content, list):
                text = _extract_text_candidate(normalized_item)
                if isinstance(text, str) and text:
                    collected_text.append(text)

    content = "\n".join(part for part in collected_text if part).strip() or None
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=str(payload.get("finish_reason") or payload.get("status") or "stop"),
        usage=_extract_usage(payload.get("usage")),
        reasoning_content=_extract_reasoning_content(payload),
    )


def _extract_tool_calls(raw_tool_calls: list[Any]) -> list[ToolCallRequest]:
    tool_calls: list[ToolCallRequest] = []
    for raw_tool_call in raw_tool_calls:
        tool_call = _normalize_payload(raw_tool_call)
        if not isinstance(tool_call, dict):
            continue

        function_payload = _normalize_payload(tool_call.get("function") or {})
        if not isinstance(function_payload, dict):
            function_payload = {}

        name = function_payload.get("name") or tool_call.get("name")
        arguments = function_payload.get("arguments")
        if arguments is None:
            arguments = tool_call.get("arguments")

        if isinstance(arguments, str):
            try:
                arguments = json_repair.loads(arguments)
            except Exception:
                arguments = {"raw": arguments}

        if not isinstance(arguments, dict):
            arguments = {}

        if not name:
            continue

        tool_calls.append(
            ToolCallRequest(
                id=str(tool_call.get("id") or tool_call.get("call_id") or name),
                name=str(name),
                arguments=arguments,
            )
        )
    return tool_calls


def _extract_usage(raw_usage: Any) -> dict[str, int]:
    usage = _normalize_payload(raw_usage)
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result


def _extract_text_candidate(payload: dict[str, Any]) -> str | None:
    for key in ("content", "text", "output_text", "response", "answer"):
        value = payload.get(key)
        if isinstance(value, str):
            return value

    raw_message = payload.get("message")
    message = _normalize_payload(raw_message) if raw_message is not None else None
    if isinstance(message, dict) and message is not payload:
        nested = _extract_text_candidate(message)
        if nested is not None:
            return nested

    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            normalized_item = _normalize_payload(item)
            if not isinstance(normalized_item, dict):
                continue
            text = normalized_item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            return "\n".join(parts)

    return None


def _extract_reasoning_content(payload: dict[str, Any]) -> str | None:
    reasoning = payload.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    return None


def _extract_error_message(payload: dict[str, Any]) -> str | None:
    error = _normalize_payload(payload.get("error"))
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("message", "error", "detail", "type"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(error, ensure_ascii=False)
    return None
