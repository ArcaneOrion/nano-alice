"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from nano_alice.providers.base import (
    LLMProvider,
    LLMResponse,
    build_provider_metadata,
    normalize_endpoint_label,
    parse_llm_response_payload,
    preview_text,
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
        resolved_model = self._normalize_model_name(model or self.default_model)
        endpoint_label = normalize_endpoint_label(self.api_base)
        kwargs: dict[str, Any] = {"model": resolved_model, "messages": messages,
                                  "max_tokens": max(1, max_tokens), "temperature": temperature}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        logger.info(
            "LLM request: provider={} requested_model={} resolved_model={} endpoint={} messages={} tools={} temperature={} max_tokens={}",
            self.__class__.__name__,
            model or self.default_model,
            resolved_model,
            endpoint_label,
            len(messages),
            len(tools or []),
            temperature,
            max_tokens,
        )
        try:
            parsed = self._parse(await self._client.chat.completions.create(**kwargs))
            parsed.provider_metadata = build_provider_metadata(
                provider_name=self.__class__.__name__,
                requested_model=model or self.default_model,
                resolved_model=resolved_model,
                endpoint=endpoint_label,
            )
            logger.info(
                "LLM response: provider={} model={} endpoint={} finish_reason={} tool_calls={} usage={} preview={}",
                parsed.provider_metadata["provider_name"],
                parsed.provider_metadata["resolved_model"],
                parsed.provider_metadata["endpoint"],
                parsed.finish_reason,
                len(parsed.tool_calls),
                parsed.usage or {},
                preview_text(parsed.content),
            )
            logger.debug(
                "LLM response details: endpoint={} tool_summary={} content={}",
                parsed.provider_metadata["endpoint"],
                summarize_tool_calls(parsed.tool_calls),
                parsed.content or "",
            )
            return parsed
        except Exception as e:
            logger.error(
                "LLM request failed: provider={} requested_model={} resolved_model={} endpoint={} error_type={} error={}",
                self.__class__.__name__,
                model or self.default_model,
                resolved_model,
                endpoint_label,
                type(e).__name__,
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
                    extra={"error_type": type(e).__name__},
                ),
            )

    def _parse(self, response: Any) -> LLMResponse:
        return parse_llm_response_payload(response)

    def get_default_model(self) -> str:
        return self.default_model
