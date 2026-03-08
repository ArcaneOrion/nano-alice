"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any
from openai import AsyncOpenAI

from nano_alice.providers.base import LLMProvider, LLMResponse, parse_llm_response_payload


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
        kwargs: dict[str, Any] = {"model": resolved_model, "messages": messages,
                                  "max_tokens": max(1, max_tokens), "temperature": temperature}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        return parse_llm_response_payload(response)

    def get_default_model(self) -> str:
        return self.default_model
