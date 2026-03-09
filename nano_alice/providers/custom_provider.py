"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

from loguru import logger
from openai import AsyncOpenAI

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


class CustomProvider(LLMProvider):

    @staticmethod
    def _normalize_model_name(model: str) -> str:
        if model.lower().startswith("openai/"):
            return model.split("/", 1)[1]
        return model

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = self._normalize_model_name(default_model)
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7) -> LLMResponse:
        messages, adaptation_summary = adapt_messages_for_visual_tool_results(messages)
        resolved_model = self._normalize_model_name(model or self.default_model)
        endpoint_label = normalize_endpoint_label(self.api_base)
        kwargs: dict[str, Any] = {"model": resolved_model, "messages": messages,
                                  "max_tokens": max(1, max_tokens), "temperature": temperature}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        request_id = new_request_id()
        request_body = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": kwargs["max_tokens"],
            "temperature": temperature,
        }
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        request_bytes = payload_bytes(request_body)
        message_summary = summarize_messages(messages)
        tool_schema_summary = summarize_tools(tools)
        logger.info(
            "LLM request: request_id={} provider={} requested_model={} resolved_model={} endpoint={} messages={} tools={} temperature={} max_tokens={} request_bytes={}",
            request_id,
            self.__class__.__name__,
            model or self.default_model,
            resolved_model,
            endpoint_label,
            len(messages),
            len(tools or []),
            temperature,
            max_tokens,
            request_bytes,
        )
        logger.debug(
            "LLM request details: request_id={} endpoint={} headers={} largest_messages={} largest_tools={}",
            request_id,
            endpoint_label,
            sanitize_headers({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            compact_summary(message_summary, name_key="role"),
            compact_summary(tool_schema_summary, name_key="name"),
        )
        try:
            raw_response_obj = await self._client.chat.completions.create(**kwargs)
            raw_response = json_ready(raw_response_obj)
            response_bytes = payload_bytes(raw_response)
            parsed = self._parse(raw_response_obj)
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": model or self.default_model,
                    "resolved_model": resolved_model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
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
                requested_model=model or self.default_model,
                resolved_model=resolved_model,
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
                "LLM response details: request_id={} endpoint={} tool_summary={} content={}",
                request_id,
                parsed.provider_metadata["endpoint"],
                summarize_tool_calls(parsed.tool_calls),
                parsed.content or "",
            )
            return parsed
        except Exception as e:
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": model or self.default_model,
                    "resolved_model": resolved_model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                    "request_body": request_body,
                    "request_summary": {
                        "request_bytes": request_bytes,
                        "messages": message_summary,
                        "tools": tool_schema_summary,
                        "message_adaptation": adaptation_summary,
                    },
                    "error": extract_exception_details(e),
                },
            )
            if trace_error:
                logger.warning("Trace unavailable for request_id={}: {}", request_id, trace_error)
            logger.error(
                "LLM request failed: request_id={} provider={} requested_model={} resolved_model={} endpoint={} request_bytes={} error_type={} trace={} error={}",
                request_id,
                self.__class__.__name__,
                model or self.default_model,
                resolved_model,
                endpoint_label,
                request_bytes,
                type(e).__name__,
                trace_path,
                str(e),
            )
            return LLMResponse(
                content=f"Error: {e}",
                finish_reason="error",
                provider_metadata=build_provider_metadata(
                    provider_name=self.__class__.__name__,
                    requested_model=model or self.default_model,
                    resolved_model=resolved_model,
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

    def _parse(self, response: Any) -> LLMResponse:
        return parse_llm_response_payload(response)

    def get_default_model(self) -> str:
        return self.default_model
