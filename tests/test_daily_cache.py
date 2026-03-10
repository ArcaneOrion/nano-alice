from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nano_alice.agent.daily_cache import DailyCacheRecorder, DailyCacheStore, TodayRecallRetriever


def test_daily_cache_store_round_trip(tmp_path: Path) -> None:
    store = DailyCacheStore(tmp_path)
    recorder = DailyCacheRecorder()
    now = datetime(2026, 3, 10, 10, 12, 33)

    records = recorder.build_records(
        session_key="cli:daily",
        trigger="帮我看看 DeepSeek 今天有没有正式发布新模型",
        tool_events=[
            {
                "name": "web_search",
                "arguments": {"query": "DeepSeek release news"},
                "result": (
                    "Results for: DeepSeek release news\n\n"
                    "1. Official Blog\n"
                    "   https://example.com/deepseek\n"
                    "   未发现正式发布公告，当前主要是媒体转载。\n"
                ),
            }
        ],
        final_content="暂未发现正式发布。",
        now=now,
    )

    path = store.append_records(records, now=now)

    assert path is not None
    assert path.name == "2026-03-10.md"
    content = path.read_text(encoding="utf-8")
    assert "# Daily Cache - 2026-03-10" in content
    assert 'input: query="DeepSeek release news"' in content
    assert "reuse_note:" in content

    loaded = store.load_today_records(now=now)
    assert len(loaded) == 1
    assert loaded[0].session_key == "cli:daily"
    assert loaded[0].source_type == "web_search"
    assert loaded[0].links == ["https://example.com/deepseek"]


def test_today_recall_retrieves_most_relevant_today_record(tmp_path: Path) -> None:
    store = DailyCacheStore(tmp_path)
    recorder = DailyCacheRecorder()
    now = datetime(2026, 3, 10, 11, 0, 0)
    records = recorder.build_records(
        session_key="cli:daily",
        trigger="帮我看看 DeepSeek 今天有没有正式发布新模型",
        tool_events=[
            {
                "name": "web_search",
                "arguments": {"query": "DeepSeek release news"},
                "result": (
                    "Results for: DeepSeek release news\n\n"
                    "1. Official Blog\n"
                    "   https://example.com/deepseek\n"
                    "   未发现正式发布公告，当前主要是媒体转载。\n"
                ),
            },
            {
                "name": "exec",
                "arguments": {"command": "python3 scripts/stock_a.py"},
                "result": "已成功获取 A 股主要指数数据。",
            },
        ],
        final_content="处理完成。",
        now=now,
    )
    store.append_records(records, now=now)

    recall = TodayRecallRetriever(store).recall(
        "DeepSeek 今天正式发布了吗",
        now=now,
        session_key="cli:daily",
    )

    assert recall is not None
    assert "web_search" in recall
    assert "正式发布公告" in recall
    assert "stock_a.py" not in recall


def test_today_recall_filters_other_sessions(tmp_path: Path) -> None:
    store = DailyCacheStore(tmp_path)
    recorder = DailyCacheRecorder()
    now = datetime(2026, 3, 10, 12, 0, 0)

    own_records = recorder.build_records(
        session_key="cli:daily",
        trigger="帮我看看 DeepSeek 今天有没有正式发布新模型",
        tool_events=[
            {
                "name": "web_search",
                "arguments": {"query": "DeepSeek release news"},
                "result": (
                    "Results for: DeepSeek release news\n\n"
                    "1. Official Blog\n"
                    "   https://example.com/deepseek\n"
                    "   未发现正式发布公告，当前主要是媒体转载。\n"
                ),
            }
        ],
        final_content="暂未发现正式发布。",
        now=now,
    )
    other_records = recorder.build_records(
        session_key="telegram:room-2",
        trigger="帮我查一下工资表是否已经更新",
        tool_events=[
            {
                "name": "exec",
                "arguments": {"command": "cat /tmp/payroll.txt"},
                "result": "工资表已更新，包含 Alice 和 Bob 的薪资数据。",
            }
        ],
        final_content="工资表已更新。",
        now=now,
    )

    store.append_records(own_records + other_records, now=now)

    recall = TodayRecallRetriever(store).recall(
        "DeepSeek 今天正式发布了吗",
        now=now,
        session_key="cli:daily",
    )

    assert recall is not None
    assert "正式发布公告" in recall
    assert "payroll" not in recall
    assert "薪资" not in recall


def test_today_recall_prefers_newest_record_when_scores_tie(tmp_path: Path) -> None:
    store = DailyCacheStore(tmp_path)
    recorder = DailyCacheRecorder()

    older = recorder.build_records(
        session_key="cli:daily",
        trigger="帮我看看 DeepSeek 今天有没有正式发布新模型",
        tool_events=[
            {
                "name": "web_search",
                "arguments": {"query": "DeepSeek release news"},
                "result": (
                    "Results for: DeepSeek release news\n\n"
                    "1. Official Blog\n"
                    "   https://example.com/deepseek\n"
                    "   上午仍未发现正式发布公告。\n"
                ),
            }
        ],
        final_content="上午暂无发布。",
        now=datetime(2026, 3, 10, 9, 0, 0),
    )
    newer = recorder.build_records(
        session_key="cli:daily",
        trigger="再查一次 DeepSeek 今天有没有正式发布新模型",
        tool_events=[
            {
                "name": "web_search",
                "arguments": {"query": "DeepSeek release news"},
                "result": (
                    "Results for: DeepSeek release news\n\n"
                    "1. Official Blog\n"
                    "   https://example.com/deepseek\n"
                    "   下午确认官网仍未发布，媒体报道已更新措辞。\n"
                ),
            }
        ],
        final_content="下午再次确认暂无发布。",
        now=datetime(2026, 3, 10, 15, 0, 0),
    )

    store.append_records(older, now=datetime(2026, 3, 10, 9, 0, 0))
    store.append_records(newer, now=datetime(2026, 3, 10, 15, 0, 0))

    recall = TodayRecallRetriever(store).recall(
        "DeepSeek 今天正式发布了吗",
        now=datetime(2026, 3, 10, 16, 0, 0),
        session_key="cli:daily",
    )

    assert recall is not None
    assert "[15:00:00] web_search: 下午确认官网仍未发布" in recall
