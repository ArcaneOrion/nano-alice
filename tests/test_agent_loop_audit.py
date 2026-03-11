import asyncio
from pathlib import Path

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.memory import MemoryStore
from nano_alice.agent.tools.base import Tool
from nano_alice.agent.tools.memory_search import _MemoryIndex
from nano_alice.bus.queue import MessageBus
from nano_alice.config.schema import MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nano_alice.session.manager import Session, SessionManager


class CaptureProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.seen_messages.append(messages)
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "fake/model"


class NoopTool(Tool):
    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "Return a fixed result."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        return f"noop:{kwargs['value']}"


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path
    (workspace / "memory").mkdir()
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    return workspace


def test_consolidated_messages_are_excluded_from_default_history_window(tmp_path: Path) -> None:
    session = Session(key="audit:history")
    for i in range(30):
        session.add_message("user", f"user-{i}")
        session.add_message("assistant", f"assistant-{i}")

    asyncio.run(MemoryStore(tmp_path).consolidate(session, memory_window=50))

    assert session.last_consolidated == 35

    history = session.get_history(max_messages=50)
    history_text = "\n".join(str(item["content"]) for item in history)

    assert "assistant-10" not in history_text
    assert "assistant-16" not in history_text
    assert "assistant-18" in history_text
    assert "assistant-29" in history_text


def test_tool_call_progress_only_emits_tool_hint_by_default(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    provider = CaptureProvider(
        [
            LLMResponse(
                content="我先把错误记录改一下。",
                tool_calls=[ToolCallRequest(id="tool-1", name="noop", arguments={"value": "fix"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="现在已经核对完了。", finish_reason="stop"),
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    loop.tools.register(NoopTool())
    progress_events: list[str] = []

    async def collect_progress(content: str) -> None:
        progress_events.append(content)

    asyncio.run(
        loop._run_agent_loop(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            on_progress=collect_progress,
        )
    )

    assert len(progress_events) == 1
    assert progress_events[0] == 'noop("fix")'


def test_memory_search_index_includes_recent_scratch_md_by_default(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    (memory_dir / "daily").mkdir(parents=True)
    (memory_dir / "SCRATCH.md").write_text(
        "# Scratch\n\n### [2026-03-11 09:18] 课表纠错\n- 专业英语在周三下午，不是周五。\n",
        encoding="utf-8",
    )
    (memory_dir / "daily" / "2026-03-11.md").write_text(
        "# Daily Cache - 2026-03-11\n\n## Records\n\n### [09:00:00] web_search\n- session_key: cli:daily\n- tags: [deepseek]\n- source_type: web_search\n- trigger: 再查一次\n- input: query=\"DeepSeek release news\"\n- brief_summary: 官网暂无正式公告\n- reuse_note: 可直接复用\n- freshness: session\n",
        encoding="utf-8",
    )

    index = _MemoryIndex(
        memory_dir=memory_dir,
        api_base="https://example.com",
        api_key="test",
        model="fake-embedding",
        dimensions=0,
        extra_headers={},
        min_score=0.0,
    )

    async def fake_batch_embed(texts: list[str], batch_size: int = 32) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "专业英语" in text else 0.0,
                    1.0 if "周三" in text else 0.0,
                    1.0 if "周五" in text else 0.0,
                    float(len(lowered) % 7),
                ]
            )
        return vectors

    index._batch_embed = fake_batch_embed  # type: ignore[method-assign]

    results = asyncio.run(index.search("专业英语 周三", top_k=3))

    matching = [result for result in results if "专业英语在周三下午" in result["text"]]
    assert len(matching) == 1
    assert matching[0]["file"] == "memory/SCRATCH.md"


def test_memory_search_index_excludes_daily_cache_by_default(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    (memory_dir / "daily").mkdir(parents=True)
    (memory_dir / "daily" / "2026-03-11.md").write_text(
        "# Daily Cache - 2026-03-11\n\n## Records\n\n### [09:00:00] web_search\n- session_key: cli:daily\n- tags: [deepseek]\n- source_type: web_search\n- trigger: 再查一次\n- input: query=\"DeepSeek release news\"\n- brief_summary: 官网暂无正式公告\n- reuse_note: 可直接复用\n- freshness: session\n",
        encoding="utf-8",
    )

    index = _MemoryIndex(
        memory_dir=memory_dir,
        api_base="https://example.com",
        api_key="test",
        model="fake-embedding",
        dimensions=0,
        extra_headers={},
        min_score=0.0,
    )

    async def fake_batch_embed(texts: list[str], batch_size: int = 32) -> list[list[float]]:
        return [[1.0, 1.0, float(len(text) % 7)] for text in texts]

    index._batch_embed = fake_batch_embed  # type: ignore[method-assign]

    results = asyncio.run(index.search("DeepSeek 官网正式公告", top_k=3))

    assert results == []
