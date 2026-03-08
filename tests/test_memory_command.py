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
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        return LLMResponse(content="normal-response")

    def get_default_model(self) -> str:
        return "fake/model"


def test_memory_command_short_circuits_normal_chat(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("facts", encoding="utf-8")

    bus = MessageBus()
    provider = FakeProvider()
    sessions = SessionManager(workspace)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=sessions,
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_maintenance():
        return {
            "status": "done",
            "files_scanned": ["memory/MEMORY.md"],
            "files_modified": ["memory/MEMORY.md"],
            "summary": "Removed duplicate facts.",
            "error": "",
        }

    loop.run_memory_maintenance = fake_maintenance  # type: ignore[method-assign]

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="user",
                chat_id="chat1",
                content="/memory",
            ),
            session_key="feishu:chat1",
        )
    )

    assert response is not None
    assert "记忆整理完成" in response.content
    assert "memory/MEMORY.md" in response.content
    assert provider.calls == 0
    assert sessions.get_or_create("feishu:chat1").messages == []


def test_memory_command_listed_in_help(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    (workspace / "memory").mkdir()

    loop = AgentLoop(
        bus=MessageBus(),
        provider=FakeProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="feishu", sender_id="user", chat_id="chat1", content="/help"),
            session_key="feishu:chat1",
        )
    )

    assert response is not None
    assert "/memory" in response.content
