from types import SimpleNamespace

from nano_alice.providers.base import parse_llm_response_payload


class DumpableResponse:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


def test_parse_plain_text_response_from_proxy():
    response = parse_llm_response_payload("现在是北京时间 10:03。")

    assert response.finish_reason == "stop"
    assert response.content == "现在是北京时间 10:03。"
    assert response.tool_calls == []


def test_parse_json_string_chat_completion_response():
    raw = '''{
        "choices": [{
            "message": {"content": "你好"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    }'''

    response = parse_llm_response_payload(raw)

    assert response.content == "你好"
    assert response.finish_reason == "stop"
    assert response.usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_parse_responses_api_payload_with_output_blocks():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "第一行"},
                    {"type": "output_text", "text": "第二行"},
                ],
            }
        ],
        "status": "completed",
        "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
    }

    response = parse_llm_response_payload(payload)

    assert response.content == "第一行\n第二行"
    assert response.finish_reason == "completed"
    assert response.usage == {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}


def test_parse_function_call_from_responses_api_payload():
    payload = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "get_time",
                "arguments": '{"timezone": "Asia/Shanghai"}',
            }
        ]
    }

    response = parse_llm_response_payload(payload)

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_123"
    assert response.tool_calls[0].name == "get_time"
    assert response.tool_calls[0].arguments == {"timezone": "Asia/Shanghai"}


def test_parse_dumpable_chat_completion_object():
    payload = {
        "choices": [
            {
                "message": {
                    "content": "ok",
                    "tool_calls": [
                        {
                            "id": "tool_1",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city": "Shanghai"}',
                            },
                        }
                    ],
                    "reasoning_content": "think",
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    response = parse_llm_response_payload(DumpableResponse(payload))

    assert response.content == "ok"
    assert response.finish_reason == "tool_calls"
    assert response.reasoning_content == "think"
    assert response.tool_calls[0].arguments == {"city": "Shanghai"}


def test_parse_error_payload():
    response = parse_llm_response_payload({"error": {"message": "bad gateway"}})

    assert response.finish_reason == "error"
    assert response.content == "Error: bad gateway"
