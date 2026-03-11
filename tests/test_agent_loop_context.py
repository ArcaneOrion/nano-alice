import asyncio
from datetime import date
from pathlib import Path

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.tools.base import Tool
from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.config.schema import MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nano_alice.session.manager import SessionManager


class FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.seen_messages = None

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.seen_messages = messages
        return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})

    def get_default_model(self) -> str:
        return "fake/model"


class SubagentFakeProvider(FakeProvider):
    def get_default_model(self) -> str:
        return "subagent/model"


class DailyRecallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)
        self.seen_messages = []
        self.search_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.seen_messages.append(messages)
        user_content = messages[-1]["content"]
        if isinstance(user_content, list):
            user_text = next(
                (item.get("text", "") for item in user_content if item.get("type") == "text"),
                "",
            )
        else:
            user_text = user_content

        if "再次确认" in user_text:
            assert "<today_recall>" in user_text
            return LLMResponse(
                content="今天已经查过一次，暂未发现官网正式发布公告。",
                finish_reason="stop",
                usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            )

        if any(msg.get("role") == "tool" for msg in messages):
            return LLMResponse(
                content="暂未发现官网正式发布公告。",
                finish_reason="stop",
                usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            )

        self.search_calls += 1
        return LLMResponse(
            content="我先搜索一下。",
            tool_calls=[
                ToolCallRequest(
                    id="tool-web-search-1",
                    name="web_search",
                    arguments={"query": "DeepSeek release news"},
                )
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class FakeWebSearchTool(Tool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "fake search"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

    async def execute(self, **kwargs):
        query = kwargs["query"]
        return (
            f"Results for: {query}\n\n"
            "1. Official Blog\n"
            "   https://example.com/deepseek\n"
            "   未发现正式发布公告，当前主要是媒体转载。\n"
        )


class SpyTodayRecall:
    def __init__(self, result: str | None = None) -> None:
        self.result = result
        self.calls: list[tuple[str, str | None]] = []

    def recall(self, query: str, now=None, *, session_key: str | None = None) -> str | None:
        del now
        self.calls.append((query, session_key))
        return self.result


def test_process_direct_reports_structured_context_metrics(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "SOUL.md").write_text("assistant soul", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("long term note", encoding="utf-8")

    bus = MessageBus()
    provider = FakeProvider()
    sessions = SessionManager(workspace)
    session = sessions.get_or_create("cli:direct")
    session.add_message("user", "older question")
    session.add_message("assistant", "older answer")
    sessions.save(session)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=sessions,
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    response = asyncio.run(
        loop._process_message(
            msg=InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="direct",
                content="new question",
                media=[],
                metadata={},
            ),
            session_key="cli:direct",
            on_progress=None,
        )
    )

    assert response is not None
    context_size = response.metadata["context_size"]
    assert context_size["system_chars"] > 0
    assert context_size["history_chars"] > 0
    assert context_size["current_context_chars"] > 0
    assert context_size["user_input_chars"] == len("new question")
    assert context_size["history_message_count"] == 2
    assert response.metadata["token_usage"]["total_tokens"] == 12

    current_user_message = provider.seen_messages[-1]["content"]
    assert "<context>" in current_user_message
    assert "<user_input>" in current_user_message
    assert "new question" in current_user_message


def test_second_turn_injects_today_recall_from_daily_cache(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "SOUL.md").write_text("assistant soul", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("long term note", encoding="utf-8")

    bus = MessageBus()
    provider = DailyRecallProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    loop.tools.register(FakeWebSearchTool())

    first_response = asyncio.run(
        loop._process_message(
                msg=InboundMessage(
                    channel="cli",
                    sender_id="user",
                    chat_id="direct",
                    content="DeepSeek 今天有没有正式发布新模型？",
                    media=[],
                    metadata={},
                ),
            session_key="cli:daily",
            on_progress=None,
        )
    )
    assert first_response is not None
    asyncio.run(loop.await_pending())

    daily_path = workspace / "memory" / "daily" / f"{date.today().isoformat()}.md"
    assert daily_path.exists()
    assert "DeepSeek release news" in daily_path.read_text(encoding="utf-8")

    second_response = asyncio.run(
        loop._process_message(
                msg=InboundMessage(
                    channel="cli",
                    sender_id="user",
                    chat_id="direct",
                    content="再次确认一下，DeepSeek 今天是不是还没有正式发布？",
                    media=[],
                    metadata={},
                ),
            session_key="cli:daily",
            on_progress=None,
        )
    )

    assert second_response is not None
    assert "暂未发现官网正式发布公告" in second_response.content
    assert provider.search_calls == 1
    second_user_message = provider.seen_messages[-1][-1]["content"]
    assert "<today_recall>" in second_user_message
    assert "DeepSeek" in second_user_message


def test_normal_conversation_does_not_load_today_recall(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    (workspace / "memory").mkdir()

    bus = MessageBus()
    provider = FakeProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(exist_ok=True)
    (daily_dir / f"{date.today().isoformat()}.md").write_text(
        "# Daily Cache - today\n\n## Records\n\n",
        encoding="utf-8",
    )
    spy = SpyTodayRecall("cached result")
    loop.today_recall = spy

    response = asyncio.run(
        loop._process_message(
            msg=InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="direct",
                content="今天有什么课吗",
                media=[],
                metadata={},
            ),
            session_key="cli:daily",
            on_progress=None,
        )
    )

    assert response is not None
    assert spy.calls == []
    current_user_message = provider.seen_messages[-1]["content"]
    assert "<today_recall>" not in current_user_message


def test_followup_query_loads_today_recall_only_when_needed(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    (workspace / "memory").mkdir()

    bus = MessageBus()
    provider = FakeProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(exist_ok=True)
    (daily_dir / f"{date.today().isoformat()}.md").write_text(
        "# Daily Cache - today\n\n## Records\n\n### [09:00:00] web_search\n- session_key: cli:daily\n- tags: [deepseek]\n- source_type: web_search\n- trigger: 再查一次\n- input: query=\"DeepSeek release news\"\n- brief_summary: 官网暂无正式公告\n- reuse_note: 可直接复用\n- freshness: session\n",
        encoding="utf-8",
    )
    spy = SpyTodayRecall("今天已经查过一次，官网暂无正式公告。")
    loop.today_recall = spy

    response = asyncio.run(
        loop._process_message(
            msg=InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="direct",
                content="再确认一下，DeepSeek 今天有没有正式发布？",
                media=[],
                metadata={},
            ),
            session_key="cli:daily",
            on_progress=None,
        )
    )

    assert response is not None
    assert spy.calls == [("再确认一下，DeepSeek 今天有没有正式发布？", "cli:daily")]
    current_user_message = provider.seen_messages[-1]["content"]
    assert "<today_recall>" in current_user_message
    assert "官网暂无正式公告" in current_user_message


def test_generic_official_site_question_does_not_trigger_unrelated_today_recall(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    (workspace / "memory").mkdir()

    bus = MessageBus()
    provider = FakeProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(exist_ok=True)
    (daily_dir / f"{date.today().isoformat()}.md").write_text(
        "# Daily Cache - today\n\n## Records\n\n### [09:00:00] web_search\n- session_key: cli:daily\n- tags: [deepseek]\n- source_type: web_search\n- trigger: 再查一次\n- input: query=\"DeepSeek release news\"\n- brief_summary: 官网暂无正式公告\n- reuse_note: 可直接复用\n- freshness: session\n",
        encoding="utf-8",
    )
    spy = SpyTodayRecall("今天已经查过一次，官网暂无正式公告。")
    loop.today_recall = spy

    response = asyncio.run(
        loop._process_message(
            msg=InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="direct",
                content="Claude 官网地址是什么？",
                media=[],
                metadata={},
            ),
            session_key="cli:daily",
            on_progress=None,
        )
    )

    assert response is not None
    assert spy.calls == []
    current_user_message = provider.seen_messages[-1]["content"]
    assert "<today_recall>" not in current_user_message


def test_agent_loop_uses_dedicated_subagent_provider_when_configured(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")

    bus = MessageBus()
    provider = FakeProvider()
    subagent_provider = SubagentFakeProvider()

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        subagent_provider=subagent_provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    assert loop.subagents.provider is subagent_provider
    assert loop.subagents.model == "subagent/model"


def test_agent_loop_preserves_explicit_model_for_subagents_without_dedicated_provider(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")

    bus = MessageBus()
    provider = FakeProvider()

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        model="override/model",
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    assert loop.subagents.provider is provider
    assert loop.subagents.model == "override/model"
