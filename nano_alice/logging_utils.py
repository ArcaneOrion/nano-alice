"""Shared logging and trace helpers."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


def preview_value(value: Any, limit: int = 200) -> str:
    """Return a compact single-line preview."""
    text = _coerce_preview_text(value)
    preview = " ".join(text.strip().split())
    if len(preview) <= limit:
        return preview
    return preview[:limit] + "..."


def json_ready(value: Any) -> Any:
    """Convert arbitrary values into JSON-serializable structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list | tuple | set):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}

    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return json_ready(method())
            except TypeError:
                return json_ready(method(exclude_none=False))
            except Exception:
                pass

    if hasattr(value, "__dict__") and isinstance(value.__dict__, dict):
        return json_ready(value.__dict__)

    return str(value)


def json_dumps(value: Any) -> str:
    """Serialize a value to JSON for size accounting and traces."""
    return json.dumps(json_ready(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_bytes(value: Any) -> int:
    """Return UTF-8 byte length of a JSON payload."""
    return len(json_dumps(value).encode("utf-8"))


def new_request_id() -> str:
    """Generate a short request id for cross-log correlation."""
    return uuid.uuid4().hex[:12]


def summarize_messages(messages: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Return per-message byte summaries."""
    summaries: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        summary = {
            "index": index,
            "role": message.get("role", "-"),
            "bytes": payload_bytes(message),
            "content_kind": _content_kind(content),
        }
        if isinstance(content, list):
            summary["items"] = len(content)
        if message.get("tool_calls"):
            summary["tool_calls"] = len(message.get("tool_calls") or [])
        if message.get("tool_call_id"):
            summary["tool_call_id"] = message.get("tool_call_id")
        text_preview = preview_value(content, limit=80)
        if text_preview:
            summary["preview"] = text_preview
        summaries.append(summary)

    summaries.sort(key=lambda item: int(item["bytes"]), reverse=True)
    return summaries[:limit]


def summarize_tools(tools: list[dict[str, Any]] | None, limit: int = 8) -> list[dict[str, Any]]:
    """Return per-tool schema byte summaries."""
    summaries: list[dict[str, Any]] = []
    for index, tool in enumerate(tools or []):
        function = tool.get("function") or {}
        summaries.append(
            {
                "index": index,
                "name": function.get("name") or tool.get("name") or "-",
                "bytes": payload_bytes(tool),
            }
        )
    summaries.sort(key=lambda item: int(item["bytes"]), reverse=True)
    return summaries[:limit]


def summarize_tool_result(tool_name: str, result: Any) -> dict[str, Any]:
    """Return a structured summary for a tool result payload."""
    ready = json_ready(result)
    summary: dict[str, Any] = {
        "tool_name": tool_name,
        "result_kind": _content_kind(ready),
        "result_bytes": payload_bytes(ready),
        "preview": preview_value(ready, limit=160),
    }
    if isinstance(ready, list):
        summary["items"] = len(ready)
    if isinstance(ready, dict):
        summary["keys"] = sorted(ready.keys())[:12]

    image_meta = _extract_image_file_meta(ready)
    if image_meta:
        summary.update(image_meta)
    return summary


def compact_summary(items: list[dict[str, Any]], *, name_key: str, bytes_key: str = "bytes", limit: int = 4) -> str:
    """Format the biggest payload items into a short log string."""
    if not items:
        return "-"
    parts: list[str] = []
    for item in items[:limit]:
        name = item.get(name_key, "-")
        size = item.get(bytes_key, 0)
        parts.append(f"{name}:{size}B")
    return ", ".join(parts)


def write_trace_file(
    namespace: str, label: str, request_id: str, payload: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Persist a full trace payload and return (file path, error)."""
    from nano_alice.config.loader import get_data_dir

    try:
        trace_dir = get_data_dir() / "logs" / "traces" / namespace
        trace_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)[:48] or "trace"
        path = trace_dir / f"{timestamp}_{safe_label}_{request_id}.json"
        path.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path), None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Trace write failed: namespace={} label={} request_id={} error={}", namespace, label, request_id, error)
        return None, error


def extract_exception_details(exc: Exception) -> dict[str, Any]:
    """Collect structured exception details for traces."""
    from nano_alice.providers.base import sanitize_headers

    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    for attr in ("status_code", "request_id", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            details[attr] = value

    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            details["response_status_code"] = status_code

        headers = getattr(response, "headers", None)
        if headers is not None:
            details["response_headers"] = sanitize_headers(dict(headers))

        text = getattr(response, "text", None)
        if isinstance(text, str):
            details["response_text"] = text
        else:
            content = getattr(response, "content", None)
            if isinstance(content, bytes):
                details["response_text"] = content.decode("utf-8", "replace")
            elif isinstance(content, str):
                details["response_text"] = content

    body = getattr(exc, "body", None)
    if body is not None:
        details["body"] = json_ready(body)
    return details


def adapt_messages_for_visual_tool_results(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand tool image references into follow-up image messages for the next model turn."""
    adapted: list[dict[str, Any]] = []
    adaptation_summary: list[dict[str, Any]] = []
    trailing_tool_indexes = _find_trailing_tool_indexes(messages)

    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            adapted.append(message)
            continue

        content = message.get("content")
        image_entries = _extract_image_file_entries(content)
        if not image_entries:
            adapted.append(message)
            continue

        text_parts = _collect_text_blocks(content)
        placeholders: list[str] = []
        converted_images: list[dict[str, Any]] = []
        status = "ok"

        for image_entry in image_entries:
            image_url, error = _image_file_to_data_url(image_entry)
            label = image_entry.get("filename") or Path(str(image_entry.get("path", ""))).name or "image"
            if image_url:
                converted_images.append({"type": "image_url", "image_url": {"url": image_url}})
                placeholders.append(
                    f"[image:{label} size={image_entry.get('size_bytes', 0)}B"
                    + (f" {image_entry['width']}x{image_entry['height']}" if image_entry.get("width") and image_entry.get("height") else "")
                    + "]"
                )
            else:
                status = "degraded"
                placeholders.append(f"[image_unavailable:{label} reason={error}]")

        tool_text = "\n".join(part for part in [*text_parts, *placeholders] if part).strip()
        adapted.append(
            {
                **message,
                "content": tool_text or json.dumps(_replace_image_files_with_placeholders(content), ensure_ascii=False),
            }
        )

        if converted_images and index in trailing_tool_indexes:
            user_content: list[dict[str, Any]] = [{"type": "text", "text": "Visual attachment from the previous tool result."}]
            user_content.extend(converted_images)
            adapted.append({"role": "user", "content": user_content})

        adaptation_summary.append(
            {
                "message_index": index,
                "tool_call_id": message.get("tool_call_id"),
                "images": len(image_entries),
                "status": status,
                "injected_images": len(converted_images) if index in trailing_tool_indexes else 0,
            }
        )

    return adapted, adaptation_summary


def _content_kind(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        value_type = value.get("type")
        if isinstance(value_type, str):
            return value_type
        return "object"
    return type(value).__name__


def _coerce_preview_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return json.dumps(json_ready(value[:2]), ensure_ascii=False)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        return json.dumps(json_ready(value), ensure_ascii=False)
    return str(value)


def _extract_image_file_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("type") == "image_file":
        result = {"path": value.get("path", "-"), "size_bytes": value.get("size_bytes", 0)}
        if value.get("width") and value.get("height"):
            result["dimensions"] = f"{value['width']}x{value['height']}"
        return result

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("type") == "image_file":
                return _extract_image_file_meta(item)
    return {}


def _find_trailing_tool_indexes(messages: list[dict[str, Any]]) -> set[int]:
    trailing_indexes: set[int] = set()
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") != "tool":
            break
        trailing_indexes.add(index)
    return trailing_indexes


def _extract_image_file_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and value.get("type") == "image_file":
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and item.get("type") == "image_file"]
    return []


def _collect_text_blocks(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            texts.append(str(item["text"]))
    return texts


def _replace_image_files_with_placeholders(value: Any) -> Any:
    if isinstance(value, list):
        replaced: list[Any] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "image_file":
                placeholder = {
                    "type": "image_file",
                    "path": item.get("path"),
                    "filename": item.get("filename"),
                    "mime_type": item.get("mime_type"),
                    "size_bytes": item.get("size_bytes"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "inlined_to_model": True,
                }
                replaced.append(placeholder)
            else:
                replaced.append(item)
        return replaced
    return value


def _image_file_to_data_url(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    path = entry.get("path")
    mime_type = entry.get("mime_type")
    if not isinstance(path, str) or not path:
        return None, "missing_path"
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        return None, "invalid_mime_type"

    try:
        file_path = Path(path).expanduser()
        image_bytes = file_path.read_bytes()
    except Exception as exc:
        return None, f"read_failed:{type(exc).__name__}"

    if len(image_bytes) > 10 * 1024 * 1024:
        return None, "image_too_large"
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}", None
