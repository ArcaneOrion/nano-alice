"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, AsyncGenerator

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

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
    ToolCallRequest,
    build_provider_metadata,
    normalize_endpoint_label,
    preview_text,
    sanitize_headers,
    summarize_tool_calls,
)

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "nano-alice"


class OpenAICodexProvider(LLMProvider):
    """Call the Responses API via OAuth or API-key-based compatible gateways."""

    def __init__(
        self,
        default_model: str = "openai-codex/gpt-5.1-codex",
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key=api_key, api_base=api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model = model or self.default_model
        resolved_model = _strip_model_prefix(model)
        messages, adaptation_summary = adapt_messages_for_visual_tool_results(messages)
        system_prompt, input_items = _convert_messages(messages)

        if self.api_key:
            headers = _build_api_key_headers(self.api_key, self.extra_headers)
            url = _resolve_responses_url(self.api_base)
        else:
            token = await asyncio.to_thread(get_codex_token)
            headers = _build_oauth_headers(token.account_id, token.access)
            url = DEFAULT_CODEX_URL
        endpoint_label = normalize_endpoint_label(url)
        request_id = new_request_id()

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": _prompt_cache_key(messages),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        if tools:
            body["tools"] = _convert_tools(tools)

        request_bytes = payload_bytes(body)
        message_summary = summarize_messages(messages)
        tool_schema_summary = summarize_tools(tools)

        logger.info(
            "LLM request: request_id={} provider={} requested_model={} resolved_model={} endpoint={} messages={} input_items={} tools={} temperature={} max_tokens={} request_bytes={}",
            request_id,
            self.__class__.__name__,
            model,
            resolved_model,
            endpoint_label,
            len(messages),
            len(input_items),
            len(tools or []),
            temperature,
            max_tokens,
            request_bytes,
        )
        logger.debug(
            "LLM request details: request_id={} endpoint={} headers={} tool_names={} largest_messages={} largest_tools={}",
            request_id,
            endpoint_label,
            sanitize_headers(headers),
            [tool.get("name") or ((tool.get("function") or {}).get("name")) or "-" for tool in (tools or [])],
            compact_summary(message_summary, name_key="role"),
            compact_summary(tool_schema_summary, name_key="name"),
        )

        try:
            try:
                codex_result = await _request_codex(url, headers, body, verify=True)
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                codex_result = await _request_codex(url, headers, body, verify=False)
            response_bytes = len(codex_result["raw_response"].encode("utf-8"))
            response = LLMResponse(
                content=codex_result["content"],
                tool_calls=codex_result["tool_calls"],
                finish_reason=codex_result["finish_reason"],
                provider_metadata=build_provider_metadata(
                    provider_name=self.__class__.__name__,
                    requested_model=model,
                    resolved_model=resolved_model,
                    endpoint=endpoint_label,
                    extra={
                        "request_id": request_id,
                        "request_bytes": request_bytes,
                        "response_bytes": response_bytes,
                    },
                ),
            )
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": model,
                    "resolved_model": resolved_model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers(headers),
                    "request_body": body,
                    "request_summary": {
                        "request_bytes": request_bytes,
                        "messages": message_summary,
                        "tools": tool_schema_summary,
                        "message_adaptation": adaptation_summary,
                    },
                    "raw_response": codex_result["raw_response"],
                    "response_headers": codex_result["response_headers"],
                    "response_status_code": codex_result["status_code"],
                    "raw_response_bytes": response_bytes,
                    "parsed_response": json_ready(response),
                },
            )
            response.provider_metadata["trace_path"] = trace_path
            response.provider_metadata["trace_error"] = trace_error
            if trace_error:
                logger.warning("Trace unavailable for request_id={}: {}", request_id, trace_error)
            logger.info(
                "LLM response: request_id={} provider={} model={} endpoint={} finish_reason={} tool_calls={} response_bytes={} trace={} preview={}",
                request_id,
                response.provider_metadata["provider_name"],
                response.provider_metadata["resolved_model"],
                response.provider_metadata["endpoint"],
                response.finish_reason,
                len(response.tool_calls),
                response_bytes,
                trace_path,
                preview_text(response.content),
            )
            logger.debug(
                "LLM response details: request_id={} endpoint={} tool_summary={} content={}",
                request_id,
                response.provider_metadata["endpoint"],
                summarize_tool_calls(response.tool_calls),
                response.content or "",
            )
            return response
        except Exception as e:
            trace_path, trace_error = write_trace_file(
                "providers",
                self.__class__.__name__,
                request_id,
                {
                    "request_id": request_id,
                    "provider": self.__class__.__name__,
                    "requested_model": model,
                    "resolved_model": resolved_model,
                    "endpoint": endpoint_label,
                    "request_headers": sanitize_headers(headers),
                    "request_body": body,
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
                model,
                resolved_model,
                endpoint_label,
                request_bytes,
                type(e).__name__,
                trace_path,
                str(e),
            )
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
                provider_metadata=build_provider_metadata(
                    provider_name=self.__class__.__name__,
                    requested_model=model,
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

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if "/" not in model:
        return model

    prefix, remainder = model.split("/", 1)
    normalized_prefix = prefix.lower().replace("-", "_")
    if normalized_prefix in {"openai_codex", "openaicodex"}:
        return remainder
    return model


def _build_oauth_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "nano-alice (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }




def _build_api_key_headers(api_key: str, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "responses=experimental",
        "User-Agent": "nano-alice (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
        **(extra_headers or {}),
    }


def _resolve_responses_url(api_base: str | None) -> str:
    if not api_base:
        return DEFAULT_CODEX_URL
    base = api_base.rstrip("/")
    if base.endswith("/responses"):
        return base
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"

async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(_friendly_error(response.status_code, text.decode("utf-8", "ignore")))
            content, tool_calls, finish_reason, raw_response = await _consume_sse(response)
            return {
                "content": content,
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
                "raw_response": raw_response,
                "status_code": response.status_code,
                "response_headers": sanitize_headers(dict(response.headers)),
            }


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                }
            )
            continue

    return system_prompt, input_items


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [line_part[5:].strip() for line_part in buffer if line_part.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str, str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"
    raw_events: list[str] = []

    async for event in _iter_sse(response):
        raw_events.append(json.dumps(event, ensure_ascii=False))
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            content += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            status = (event.get("response") or {}).get("status")
            finish_reason = _map_finish_reason(status)
        elif event_type in {"error", "response.failed"}:
            raise RuntimeError("Codex response failed")

    return content, tool_calls, finish_reason, "\n".join(raw_events)


_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    return f"HTTP {status_code}: {raw}"
