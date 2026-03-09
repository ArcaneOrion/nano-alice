"""Provider wrapper with primary + fallback failover behavior."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from loguru import logger

from nano_alice.providers.base import LLMProvider, LLMResponse, normalize_endpoint_label


class RotatingProvider(LLMProvider):
    """Use a primary provider and fail over through an ordered fallback pool."""

    def __init__(
        self,
        primary_provider: LLMProvider,
        fallback_providers: list[LLMProvider] | None = None,
        *,
        cooldown_seconds: float = 15 * 60,
        primary_timeout_seconds: float | None = None,
        fallback_timeout_seconds: float = 30.0,
        time_fn: Callable[[], float] | None = None,
    ):
        super().__init__()
        self.primary_provider = primary_provider
        self.fallback_providers = fallback_providers or []
        self.providers = [primary_provider, *self.fallback_providers]
        self.cooldown_seconds = cooldown_seconds
        self.primary_timeout_seconds = primary_timeout_seconds
        self.fallback_timeout_seconds = fallback_timeout_seconds
        self._time_fn = time_fn or time.monotonic
        self._active_fallback_index: int | None = None
        self._fallback_started_at: float | None = None
        logger.info(
            "Endpoint pool initialized: primary={} fallback_count={} primary_timeout_seconds={} fallback_timeout_seconds={} pool={}",
            self._provider_label(self.primary_provider),
            len(self.fallback_providers),
            self.primary_timeout_seconds,
            self.fallback_timeout_seconds,
            [self._provider_label(provider) for provider in self.providers],
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        now = self._time_fn()
        logger.info(
            "Endpoint pool state: active_fallback={} cooldown_started_at={} cooldown_seconds={}",
            self._active_fallback_index,
            self._fallback_started_at,
            self.cooldown_seconds,
        )
        if self._should_retry_primary(now):
            logger.info("Endpoint pool retrying primary after cooldown")
            self._clear_fallback_state()

        if self._active_fallback_index is None:
            primary_response = await self._call_provider(
                self.primary_provider,
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_seconds=self.primary_timeout_seconds,
            )
            if primary_response.finish_reason != "error":
                return primary_response
            logger.warning(
                "Primary endpoint failed, switching to fallback pool: primary={} request_id={} trace={} error={}",
                self._provider_label(self.primary_provider),
                (primary_response.provider_metadata or {}).get("request_id", "-"),
                (primary_response.provider_metadata or {}).get("trace_path", "-"),
                (primary_response.content or "")[:200],
            )
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
            logger.info(
                "Endpoint pool attempt: fallback_index={} offset={} provider={}",
                index,
                offset,
                self._provider_label(self.fallback_providers[index]),
            )
            fallback_response = await self._call_provider(
                self.fallback_providers[index],
                messages=messages,
                tools=tools,
                model=None,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_seconds=self.fallback_timeout_seconds,
            )
            if fallback_response.finish_reason != "error":
                self._active_fallback_index = index
                self._fallback_started_at = started_at
                logger.info(
                    "Endpoint pool locked on fallback: index={} provider={} started_at={}",
                    index,
                    self._provider_label(self.fallback_providers[index]),
                    started_at,
                )
                return fallback_response
            logger.warning(
                "Endpoint pool fallback failed: index={} provider={} request_id={} trace={} error={}",
                index,
                self._provider_label(self.fallback_providers[index]),
                (fallback_response.provider_metadata or {}).get("request_id", "-"),
                (fallback_response.provider_metadata or {}).get("trace_path", "-"),
                (fallback_response.content or "")[:200],
            )
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
        timeout_seconds: float | None,
    ) -> LLMResponse:
        try:
            call = provider.chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if timeout_seconds is not None:
                return await asyncio.wait_for(call, timeout=timeout_seconds)
            return await call
        except TimeoutError:
            logger.warning(
                "Endpoint request timed out: provider={} timeout_seconds={}",
                self._provider_label(provider),
                timeout_seconds,
            )
            return LLMResponse(
                content=f"Error: request timed out after {timeout_seconds:.1f}s",
                finish_reason="error",
            )
        except Exception as exc:
            logger.error(
                "Endpoint provider raised exception: provider={} error_type={} error={}",
                self._provider_label(provider),
                type(exc).__name__,
                exc,
            )
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

    @staticmethod
    def _provider_label(provider: LLMProvider) -> str:
        model = provider.get_default_model()
        endpoint = normalize_endpoint_label(getattr(provider, "api_base", None))
        return f"{provider.__class__.__name__}(model={model}, endpoint={endpoint})"
