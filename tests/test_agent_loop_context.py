import asyncio
from pathlib import Path

from nano_alice.agent.loop import AgentLoop
from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.config.schema import MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse
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
