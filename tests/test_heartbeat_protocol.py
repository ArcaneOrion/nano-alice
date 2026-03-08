from nano_alice.heartbeat.service import (
    HEARTBEAT_OK_TOKEN,
    HeartbeatDecision,
    normalize_heartbeat_response,
    parse_heartbeat_decision,
)


def test_parse_heartbeat_decision_push_json() -> None:
    decision = parse_heartbeat_decision(
        '{"should_push": true, "reason": "price moved", "content": "Market alert"}'
    )

    assert decision == HeartbeatDecision(
        should_push=True,
        reason="price moved",
        content="Market alert",
    )


def test_parse_heartbeat_decision_no_push_json() -> None:
    decision = parse_heartbeat_decision(
        '{"should_push": false, "reason": "no change", "content": ""}'
    )

    assert decision == HeartbeatDecision(
        should_push=False,
        reason="no change",
        content="",
    )


def test_parse_heartbeat_decision_rejects_missing_push_content() -> None:
    decision = parse_heartbeat_decision(
        '{"should_push": true, "reason": "something happened", "content": ""}'
    )

    assert decision is None


def test_normalize_heartbeat_response_returns_ok_token_for_no_push() -> None:
    decision, normalized = normalize_heartbeat_response(
        '{"should_push": false, "reason": "quiet window", "content": ""}'
    )

    assert decision is not None
    assert decision.should_push is False
    assert normalized == HEARTBEAT_OK_TOKEN


def test_normalize_heartbeat_response_keeps_push_content() -> None:
    decision, normalized = normalize_heartbeat_response(
        '{"should_push": true, "reason": "event due", "content": "Data release due now"}'
    )

    assert decision is not None
    assert decision.should_push is True
    assert normalized == "Data release due now"


def test_normalize_heartbeat_response_falls_back_to_raw_text() -> None:
    raw = "正文提到了 HEARTBEAT_OK，但这不是结构化结果"
    decision, normalized = normalize_heartbeat_response(raw)

    assert decision is None
    assert normalized == raw
