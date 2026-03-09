"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

from loguru import logger

from nano_alice.logging_utils import (
    adapt_messages_for_visual_tool_results,
    compact_summary,
    extract_exception_details,
    json_ready,
    new_request_id,
    payload_bytes,
    summarize_messages,
    summarize_tools,
    write_trace_file,
)
from nano_alice.providers.base import (
    LLMProvider,
    LLMResponse,
    build_provider_metadata,
    normalize_endpoint_label,
    parse_llm_response_payload,
    preview_text,
    sanitize_headers,
    summarize_tool_calls,
)
from nano_alice.providers.registry import find_by_model, find_gateway

# Patch httpx to allow UTF-8 in response headers (needed for some proxy servers)
from nano_alice.utils import patch_httpx_for_utf8_headers

patch_httpx_for_utf8_headers()

import litellm  # noqa: E402,I001
from litellm import acompletion  # noqa: E402,I001


# Standard OpenAI chat-completion message keys; extras (e.g. reasoning_content) are stripped for strict providers.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks."""
        if self._gateway is not None:
            return self._gateway.supports_prompt_caching
        spec = find_by_model(model)
        return spec is not None and spec.supports_prompt_caching

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected."""
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}

        return new_messages, new_tools

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in _ALLOWED_MSG_KEYS}
            # Strict providers require "content" even when assistant only has tool_calls
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        original_model = model or self.default_model
        model = self._resolve_model(original_model)
        endpoint_label = normalize_endpoint_label(self.api_base or getattr(litellm, "api_base", None))

        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)

        messages, adaptation_summary = adapt_messages_for_visual_tool_results(messages)

        # Clamp max_tokens to at least 1 — negative or zero values cause
        # LiteLLM to reject the request with "max_tokens must be at least 1".
        max_tokens = max(1, max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)

        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        request_id = new_request_id()
        request_body = {
            "model": kwargs["model"],
            "messages": kwargs["messages"],
            "max_tokens": kwargs["max_tokens"],
            "temperature": kwargs["temperature"],
        }
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        request_bytes = payload_bytes(request_body)
        message_summary = summarize_messages(kwargs["messages"])
        tool_schema_summary = summarize_tools(tools)

        logger.info(
            "LLM request: request_id={} provider={} requested_model={} resolved_model={} endpoint={} messages={} tools={} temperature={} max_tokens={} request_bytes={}",
            request_id,
            self.__class__.__name__,
            original_model,
            model,
            endpoint_label,
            len(kwargs["messages"]),
            len(tools or []),
            temperature,
            max_tokens,
            request_bytes,
        )
        logger.debug(
            "LLM request details: request_id={} endpoint={} extra_headers={} tool_names={} largest_messages={} largest_tools={}",
            request_id,
            endpoint_label,
            sanitize_headers(self.extra_headers),
            [((tool.get("function") or {}).get("name") or tool.get("name") or "-") for tool in (tools or [])],
            compact_summary(message_summary, name_key="role"),
            compact_summary(tool_schema_summary, name_key="name"),
        )

        try:
            response = await acompletion(**kwargs)
            raw_response = json_ready(response)
            response_bytes = payload_bytes(raw_response)
            parsed = self._parse_response(response)
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": original_model,
                    "resolved_model": model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers(self.extra_headers),
                    "request_body": request_body,
                    "request_summary": {
                        "request_bytes": request_bytes,
                        "messages": message_summary,
                        "tools": tool_schema_summary,
                        "message_adaptation": adaptation_summary,
                    },
                    "raw_response": raw_response,
                    "raw_response_bytes": response_bytes,
                    "parsed_response": json_ready(parsed),
                },
            )
            parsed.provider_metadata = build_provider_metadata(
                provider_name=self.__class__.__name__,
                requested_model=original_model,
                resolved_model=model,
                endpoint=endpoint_label,
                extra={
                    "request_id": request_id,
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "trace_path": trace_path,
                    "trace_error": trace_error,
                },
            )
            if trace_error:
                logger.warning("Trace unavailable for request_id={}: {}", request_id, trace_error)
            logger.info(
                "LLM response: request_id={} provider={} model={} endpoint={} finish_reason={} tool_calls={} usage={} response_bytes={} trace={} preview={}",
                request_id,
                parsed.provider_metadata["provider_name"],
                parsed.provider_metadata["resolved_model"],
                parsed.provider_metadata["endpoint"],
                parsed.finish_reason,
                len(parsed.tool_calls),
                parsed.usage or {},
                response_bytes,
                trace_path,
                preview_text(parsed.content),
            )
            logger.debug(
                "LLM response details: request_id={} endpoint={} tool_summary={} content={} reasoning={}",
                request_id,
                parsed.provider_metadata["endpoint"],
                summarize_tool_calls(parsed.tool_calls),
                parsed.content or "",
                parsed.reasoning_content or "",
            )
            return parsed
        except Exception as e:
            error_details = extract_exception_details(e)
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": original_model,
                    "resolved_model": model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers(self.extra_headers),
                    "request_body": request_body,
                    "request_summary": {
                        "request_bytes": request_bytes,
                        "messages": message_summary,
                        "tools": tool_schema_summary,
                        "message_adaptation": adaptation_summary,
                    },
                    "error": error_details,
                },
            )
            if trace_error:
                logger.warning("Trace unavailable for request_id={}: {}", request_id, trace_error)
            logger.error(
                "LLM request failed: request_id={} provider={} requested_model={} resolved_model={} endpoint={} request_bytes={} error_type={} trace={} error={}",
                request_id,
                self.__class__.__name__,
                original_model,
                model,
                endpoint_label,
                request_bytes,
                type(e).__name__,
                trace_path,
                str(e),
            )
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
                provider_metadata=build_provider_metadata(
                    provider_name=self.__class__.__name__,
                    requested_model=original_model,
                    resolved_model=model,
                    endpoint=endpoint_label,
                    extra={
                        "error_type": type(e).__name__,
                        "request_id": request_id,
                        "request_bytes": request_bytes,
                        "trace_path": trace_path,
                        "trace_error": trace_error,
                    },
                ),
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        return parse_llm_response_payload(response)

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
