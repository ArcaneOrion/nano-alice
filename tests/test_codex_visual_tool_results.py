from nano_alice.logging_utils import adapt_messages_for_visual_tool_results
from nano_alice.providers.openai_codex_provider import _convert_messages


def test_codex_convert_messages_keeps_visual_tool_result_as_input_image(tmp_path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x04\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    adapted, _ = adapt_messages_for_visual_tool_results(
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
                    }
                ],
            }
        ]
    )

    _, input_items = _convert_messages(adapted)

    assert input_items[0]["type"] == "function_call_output"
    assert input_items[1]["role"] == "user"
    assert input_items[1]["content"][1]["type"] == "input_image"
