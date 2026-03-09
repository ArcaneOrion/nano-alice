import json
from pathlib import Path

from nano_alice.logging_utils import (
    adapt_messages_for_visual_tool_results,
    extract_exception_details,
    payload_bytes,
    summarize_messages,
    summarize_tool_result,
    write_trace_file,
)


def test_write_trace_file_persists_full_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nano_alice.config.loader.get_data_dir", lambda: tmp_path)

    trace_path, trace_error = write_trace_file(
        "providers",
        "LiteLLMProvider",
        "req123",
        {"request": {"messages": [{"role": "user", "content": "hello"}]}, "response": {"content": "world"}},
    )

    assert trace_error is None
    saved = json.loads(Path(trace_path).read_text())
    assert saved["request"]["messages"][0]["content"] == "hello"
    assert saved["response"]["content"] == "world"


def test_summarize_messages_uses_real_bytes() -> None:
    messages = [
        {"role": "system", "content": "tiny"},
        {"role": "user", "content": [{"type": "text", "text": "x" * 200}]},
    ]

    summary = summarize_messages(messages)

    assert summary[0]["role"] == "user"
    assert summary[0]["bytes"] > summary[1]["bytes"]


def test_summarize_tool_result_reports_image_metadata() -> None:
    result = [
        {
            "type": "image_file",
            "path": "/tmp/test.png",
            "filename": "test.png",
            "mime_type": "image/png",
            "size_bytes": 123,
            "sha256": "abc",
            "width": 1,
            "height": 1,
        }
    ]

    summary = summarize_tool_result("read_file", result)

    assert summary["result_kind"] == "list"
    assert summary["path"] == "/tmp/test.png"
    assert summary["dimensions"] == "1x1"
    assert summary["result_bytes"] == payload_bytes(result)


def test_write_trace_file_is_non_fatal_on_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nano_alice.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "write_text", lambda self, content, encoding=None: (_ for _ in ()).throw(OSError("disk full")))

    trace_path, trace_error = write_trace_file("providers", "LiteLLMProvider", "req123", {"hello": "world"})

    assert trace_path is None
    assert "disk full" in (trace_error or "")


def test_extract_exception_details_sanitizes_response_headers() -> None:
    class _Response:
        status_code = 401
        headers = {"set-cookie": "secret", "x-trace": "abc"}
        text = "bad"

    class _TestError(Exception):
        response = _Response()

    details = extract_exception_details(_TestError("boom"))

    assert details["response_headers"]["set-cookie"] == "***"
    assert details["response_headers"]["x-trace"] == "abc"


def test_adapt_messages_for_visual_tool_results_injects_followup_image(tmp_path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x04\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    adapted, summary = adapt_messages_for_visual_tool_results(
        [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": [
                    {
                        "type": "image_file",
                        "path": str(image_path),
                        "filename": "sample.png",
                        "mime_type": "image/png",
                        "size_bytes": image_path.stat().st_size,
                        "width": 1,
                        "height": 1,
                    },
                    {"type": "text", "text": "Image file: sample.png"},
                ],
            }
        ]
    )

    assert len(adapted) == 2
    assert adapted[0]["role"] == "tool"
    assert isinstance(adapted[0]["content"], str)
    assert adapted[1]["role"] == "user"
    assert adapted[1]["content"][1]["type"] == "image_url"
    assert adapted[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert summary[0]["injected_images"] == 1


def test_adapt_messages_for_visual_tool_results_does_not_reinject_historical_images(tmp_path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x04\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    adapted, summary = adapt_messages_for_visual_tool_results(
        [
            {"role": "assistant", "content": "让我看下图片"},
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": [
                    {
                        "type": "image_file",
                        "path": str(image_path),
                        "filename": "sample.png",
                        "mime_type": "image/png",
                        "size_bytes": image_path.stat().st_size,
                        "width": 1,
                        "height": 1,
                    },
                    {"type": "text", "text": "Image file: sample.png"},
                ],
            },
            {"role": "assistant", "content": "我看到了"},
            {"role": "user", "content": "继续"},
        ]
    )

    assert len(adapted) == 4
    assert adapted[1]["role"] == "tool"
    assert isinstance(adapted[1]["content"], str)
    assert adapted[-1] == {"role": "user", "content": "继续"}
    assert summary[0]["injected_images"] == 0
