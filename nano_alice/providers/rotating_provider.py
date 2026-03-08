"""Provider wrapper with primary + fallback failover behavior."""

from __future__ import annotations

import time
from typing import Any, Callable

from nano_alice.providers.base import LLMProvider, LLMResponse


class RotatingProvider(LLMProvider):
    """Use a primary provider and fail over through an ordered fallback pool."""

    def __init__(
        self,
        primary_provider: LLMProvider,
        fallback_providers: list[LLMProvider] | None = None,
        *,
        cooldown_seconds: float = 15 * 60,
        time_fn: Callable[[], float] | None = None,
    ):
        super().__init__()
        self.primary_provider = primary_provider
        self.fallback_providers = fallback_providers or []
        self.providers = [primary_provider, *self.fallback_providers]
        self.cooldown_seconds = cooldown_seconds
        self._time_fn = time_fn or time.monotonic
        self._active_fallback_index: int | None = None
        self._fallback_started_at: float | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        now = self._time_fn()
        if self._should_retry_primary(now):
            self._clear_fallback_state()

        if self._active_fallback_index is None:
            primary_response = await self._call_provider(
                self.primary_provider,
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if primary_response.finish_reason != "error":
                return primary_response
            return await self._try_fallbacks(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                last_error=primary_response,
                started_at=now,
                start_index=0,
            )

        return await self._try_fallbacks(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            last_error=None,
            started_at=self._fallback_started_at or now,
            start_index=self._active_fallback_index,
        )

    async def _try_fallbacks(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        last_error: LLMResponse | None,
        started_at: float,
        start_index: int,
    ) -> LLMResponse:
        if not self.fallback_providers:
            return last_error or LLMResponse(content="Error: no fallback provider available", finish_reason="error")

        total = len(self.fallback_providers)
        for offset in range(total):
            index = (start_index + offset) % total
            fallback_response = await self._call_provider(
                self.fallback_providers[index],
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if fallback_response.finish_reason != "error":
                self._active_fallback_index = index
                self._fallback_started_at = started_at
                return fallback_response
            last_error = fallback_response

        return last_error or LLMResponse(content="Error: no fallback provider available", finish_reason="error")

    async def _call_provider(
        self,
        provider: LLMProvider,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        try:
            return await provider.chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            return LLMResponse(content=f"Error: {exc}", finish_reason="error")

    def _should_retry_primary(self, now: float) -> bool:
        return (
            self._active_fallback_index is not None
            and self._fallback_started_at is not None
            and now - self._fallback_started_at >= self.cooldown_seconds
        )

    def _clear_fallback_state(self) -> None:
        self._active_fallback_index = None
        self._fallback_started_at = None

    def get_default_model(self) -> str:
        return self.primary_provider.get_default_model()
