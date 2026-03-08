import asyncio
from pathlib import Path

from nano_alice.agent.memory_agent import _MAINTENANCE_SYSTEM_PROMPT, _SYSTEM_PROMPT, MemoryAgent
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FakeProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="Nothing notable")

    def get_default_model(self) -> str:
        return "fake/model"


def make_agent(tmp_path: Path) -> MemoryAgent:
    return MemoryAgent(
        provider=FakeProvider(),
        workspace=tmp_path,
        model="fake/model",
        embeddings_config=None,
    )


def test_scope_prompt_documents_managed_files() -> None:
    assert "Do NOT write `memory/YYYY-MM-DD.md` in this task." in _SYSTEM_PROMPT
    assert "important event / confirmation / failure / system event → `memory/HISTORY.md`" in _SYSTEM_PROMPT
    assert "prefer updating existing entries" in _SYSTEM_PROMPT


def test_maintenance_prompt_is_conservative() -> None:
    assert "Do NOT invent any new facts." in _MAINTENANCE_SYSTEM_PROMPT
    assert "Only use facts already present" in _MAINTENANCE_SYSTEM_PROMPT
    assert "prefer removing it from long-term files or downgrading it" in _MAINTENANCE_SYSTEM_PROMPT


def test_projects_file_rejects_append(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    result = asyncio.run(
        agent._execute_tool(
            "append_file",
            {"path": "memory/projects.md", "content": "- new status\n"},
        )
    )

    assert result == "Error: use edit_file or write_file for memory/projects.md"
    assert not (tmp_path / "memory" / "projects.md").exists()


def test_history_file_allows_append(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    result = asyncio.run(
        agent._execute_tool(
            "append_file",
            {"path": "memory/HISTORY.md", "content": "[2026-03-08] important event\n"},
        )
    )

    assert result.startswith("Appended ")
    assert (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8") == (
        "[2026-03-08] important event\n"
    )


def test_daily_log_write_is_blocked(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    result = asyncio.run(
        agent._execute_tool(
            "write_file",
            {"path": "memory/2026-03-08.md", "content": "daily note"},
        )
    )

    assert result == "Error: daily log writes are out of scope"
    assert not (tmp_path / "memory" / "2026-03-08.md").exists()


def test_scratch_write_blocked_outside_cleanup(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    result = asyncio.run(
        agent._execute_tool(
            "write_file",
            {"path": "memory/SCRATCH.md", "content": "overwrite"},
        )
    )

    assert result == "Error: SCRATCH.md is append-only outside cleanup"


def test_scratch_write_allowed_during_cleanup(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._allow_scratch_overwrite = True

    result = asyncio.run(
        agent._execute_tool(
            "write_file",
            {"path": "memory/SCRATCH.md", "content": "condensed"},
        )
    )

    assert result.startswith("Written ")
    assert (tmp_path / "memory" / "SCRATCH.md").read_text(encoding="utf-8") == "condensed"


class MaintenanceProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="Reading and reconciling memory.",
                tool_calls=[
                    ToolCallRequest(id="1", name="read_file", arguments={"path": "memory/MEMORY.md"}),
                    ToolCallRequest(id="2", name="write_file", arguments={"path": "memory/HISTORY.md", "content": "clean history\n"}),
                ],
            )
        return LLMResponse(content="Maintenance complete.")

    def get_default_model(self) -> str:
        return "fake/model"


def test_maintenance_mode_can_rewrite_history(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("fact", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("noisy history", encoding="utf-8")

    agent = MemoryAgent(
        provider=MaintenanceProvider(),
        workspace=tmp_path,
        model="fake/model",
        embeddings_config=None,
    )

    summary = asyncio.run(agent.run_maintenance())

    assert summary == "Maintenance complete."
    assert (memory_dir / "HISTORY.md").read_text(encoding="utf-8") == "clean history\n"
