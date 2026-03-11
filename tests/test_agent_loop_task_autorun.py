import asyncio
from pathlib import Path

import pytest

from nano_alice.agent.loop import AgentLoop
from nano_alice.agent.subagent import SubagentManager
from nano_alice.agent.task_state import build_steps, sync_task_pointers
from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.config.schema import MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nano_alice.session.manager import SessionManager


class AutorunProvider(LLMProvider):
    def __init__(self, *, spawn_first: bool = False):
        super().__init__(api_key=None, api_base=None)
        self.calls = []
        self.spawn_first = spawn_first

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        if self.spawn_first and not any(msg.get("role") == "tool" for msg in messages):
            return LLMResponse(
                content="我先把重任务交给后台处理。",
                tool_calls=[
                    ToolCallRequest(
                        id="tool-spawn-1",
                        name="spawn",
                        arguments={"task": "深入实现当前步骤", "label": "实现当前步骤"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        if self.spawn_first:
            return LLMResponse(
                content="已委派后台任务，等待结果。",
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        return LLMResponse(
            content="已完成当前步骤：分析需求。",
            finish_reason="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class MessageToolAutorunProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        if not any(msg.get("role") == "tool" for msg in messages):
            return LLMResponse(
                content="我来直接发送当前步骤结果。",
                tool_calls=[
                    ToolCallRequest(
                        id="tool-message-1",
                        name="message",
                        arguments={"content": "已通过 message 工具发送当前步骤结果。"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        return LLMResponse(
            content="已发送当前步骤结果。",
            finish_reason="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class SequencedAutorunProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []
        self.execution_count = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        self.execution_count += 1
        return LLMResponse(
            content=f"已完成当前步骤：步骤{self.execution_count}。",
            finish_reason="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class SpawnThenMessageProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []
        self.execution_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        self.execution_calls += 1
        if self.execution_calls == 1:
            return LLMResponse(
                content="我先委派后台任务。",
                tool_calls=[
                    ToolCallRequest(
                        id="tool-spawn-1",
                        name="spawn",
                        arguments={"task": "深入实现当前步骤", "label": "实现当前步骤"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        return LLMResponse(
            content="我来直接给用户发结果。",
            tool_calls=[
                ToolCallRequest(
                    id="tool-message-1",
                    name="message",
                    arguments={"content": "这条消息不该在 spawn 同回合发出。"},
                )
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class SameTurnSpawnThenMessageProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []
        self.execution_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        self.execution_calls += 1
        return LLMResponse(
            content="我先委派后台任务，再尝试补一句同步。",
            tool_calls=[
                ToolCallRequest(
                    id="tool-spawn-1",
                    name="spawn",
                    arguments={"task": "深入实现当前步骤", "label": "实现当前步骤"},
                ),
                ToolCallRequest(
                    id="tool-message-1",
                    name="message",
                    arguments={"content": "这条消息不该在 spawn 后继续发出。"},
                ),
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class SameTurnMessageThenSpawnProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []
        self.execution_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"逐步推进","steps":["分析需求","实现状态","补测试"]}',
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        self.execution_calls += 1
        return LLMResponse(
            content="我先同步一句当前进展，再委派后台任务。",
            tool_calls=[
                ToolCallRequest(
                    id="tool-message-1",
                    name="message",
                    arguments={"content": "先同步一句当前进展。"},
                ),
                ToolCallRequest(
                    id="tool-spawn-1",
                    name="spawn",
                    arguments={"task": "深入实现当前步骤", "label": "实现当前步骤"},
                ),
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class SubagentErrorProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(
            content="Error: request timed out after 30.0s",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class StaticSubagentResultProvider(LLMProvider):
    def __init__(self, content: str):
        super().__init__(api_key=None, api_base=None)
        self.content = content

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(
            content=self.content,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def get_default_model(self) -> str:
        return "fake/model"


class RaisingSubagentProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise RuntimeError("provider crashed")

    def get_default_model(self) -> str:
        return "fake/model"


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("long term note", encoding="utf-8")
    return workspace


def test_user_task_schedules_silent_continuation(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
        )
    )

    assert response is None
    assert bus.inbound_size == 1

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "executing"
    assert task_state.current_step_index == 2
    assert task_state.continuation_scheduled is True

    scheduled = asyncio.run(bus.consume_inbound())
    assert scheduled.channel == "system"
    assert scheduled.sender_id == "self"
    assert scheduled.metadata["_task_continue"] is True
    assert scheduled.metadata["_silent"] is True


def test_process_direct_keeps_first_task_reply_when_continuation_is_scheduled(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    response = asyncio.run(loop.process_direct("请帮我实现任务状态"))

    assert response == "已完成当前步骤：分析需求。"
    assert bus.inbound_size == 1

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "executing"
    assert task_state.current_step_index == 2
    assert task_state.continuation_scheduled is True


def test_process_direct_returns_message_tool_content(tmp_path: Path) -> None:
    """process_direct() should return message tool content when autorun uses it."""
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = MessageToolAutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    # Direct call via process_direct should return the message tool content
    response = asyncio.run(loop.process_direct("请帮我实现任务状态"))

    assert response == "已通过 message 工具发送当前步骤结果。"
    # Bus has outbound messages (for bus-driven channels) and schedules continuation
    assert bus.inbound_size == 1

    # Task continues in background
    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "executing"


def test_spawn_marks_task_waiting_for_subagent(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider(spawn_first=True)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
        )
    )

    assert response is None
    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]
    assert task_state.steps[0].status == "waiting"
    assert task_state.steps[0].spawn_task_id == "sg-001"
    assert bus.inbound_size == 0


def test_process_direct_returns_waiting_message_for_subagent_step(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider(spawn_first=True)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    response = asyncio.run(loop.process_direct("请帮我实现任务状态"))

    assert response == "已委派后台任务，等待结果。"
    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]
    assert bus.inbound_size == 0


def test_task_mode_spawn_stops_before_follow_up_tool_turn(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SpawnThenMessageProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    async def no_progress(_: str) -> None:
        return None

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
            on_progress=no_progress,
        )
    )

    assert response is None
    assert provider.execution_calls == 1
    assert bus.outbound_size == 0

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]


def test_task_mode_spawn_skips_later_tool_calls_in_same_response(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SameTurnSpawnThenMessageProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    async def no_progress(_: str) -> None:
        return None

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
            on_progress=no_progress,
        )
    )

    assert response is None
    assert provider.execution_calls == 1
    assert bus.outbound_size == 0

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]


def test_process_direct_prefers_waiting_message_when_message_and_spawn_share_turn(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SameTurnMessageThenSpawnProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    async def no_progress(_: str) -> None:
        return None

    response = asyncio.run(loop.process_direct("请帮我实现任务状态", on_progress=no_progress))

    assert response == "已委派后台任务，等待结果。"
    assert bus.outbound_size == 1
    outbound = asyncio.run(bus.consume_outbound())
    assert outbound.content == "先同步一句当前进展。"

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]


def test_bus_channel_stays_quiet_after_message_and_spawn_same_turn(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SameTurnMessageThenSpawnProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def fake_spawn(**kwargs):
        return {
            "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-001\nstatus: started",
            "label": "实现当前步骤",
            "id": "sg-001",
            "status": "started",
        }

    spawn_tool = loop.tools.get("spawn")
    assert spawn_tool is not None
    spawn_tool._manager.spawn = fake_spawn  # type: ignore[attr-defined]

    async def no_progress(_: str) -> None:
        return None

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="user",
                chat_id="ou_user_1",
                content="请帮我实现任务状态",
            ),
            session_key="feishu:ou_user_1",
            on_progress=no_progress,
        )
    )

    assert response is None
    assert bus.outbound_size == 1
    outbound = asyncio.run(bus.consume_outbound())
    assert outbound.channel == "feishu"
    assert outbound.chat_id == "ou_user_1"
    assert outbound.content == "先同步一句当前进展。"

    task_state = loop.task_states.load_active("feishu:ou_user_1")
    assert task_state is not None
    assert task_state.phase == "waiting_subagent"
    assert task_state.pending_subagent_ids == ["sg-001"]


def test_duplicate_subagent_result_is_ignored(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = loop.task_states.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.steps = build_steps(["分析需求", "实现状态"])
    task.steps[0].status = "waiting"
    task.steps[0].executor = "subagent"
    task.steps[0].spawn_task_id = "sg-001"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    sync_task_pointers(task)
    loop.task_states.save_active(task)

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:direct",
                content="后台任务结果",
                metadata={
                    "_task_id": task.task_id,
                    "_subagent_result": True,
                    "_subagent_task_id": "sg-other",
                    "_subagent_status": "ok",
                    "_silent": True,
                    "_session_key": "cli:direct",
                },
            )
        )
    )

    assert response is None
    loaded = loop.task_states.load_active("cli:direct")
    assert loaded is not None
    assert loaded.phase == "waiting_subagent"
    assert loaded.pending_subagent_ids == ["sg-001"]
    assert loaded.steps[0].status == "waiting"


def test_stale_continuation_does_not_resume_different_active_task(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    stale_task = loop.task_states.create_new_task("cli:direct", "旧任务")
    stale_task.task_id = "task-stale"
    stale_task.phase = "executing"
    stale_task.steps = build_steps(["分析需求", "实现状态"])
    stale_task.steps[0].status = "in_progress"
    sync_task_pointers(stale_task)

    active_task = loop.task_states.create_new_task("cli:direct", "新任务")
    active_task.task_id = "task-active"
    active_task.phase = "executing"
    active_task.steps = build_steps(["实现修复", "补测试"])
    active_task.steps[0].status = "in_progress"
    active_task.continuation_scheduled = True
    active_task.last_event = "continuation_scheduled:user"
    sync_task_pointers(active_task)
    loop.task_states.save_active(active_task)

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="self",
                chat_id="cli:direct",
                content="Continue active task using current task_state. Execute only the current allowed step.",
                metadata={
                    "_task_continue": True,
                    "_task_id": stale_task.task_id,
                    "_silent": True,
                    "_session_key": "cli:direct",
                },
            )
        )
    )

    assert response is None
    assert provider.calls == []

    loaded = loop.task_states.load_active("cli:direct")
    assert loaded is not None
    assert loaded.task_id == active_task.task_id
    assert loaded.continuation_scheduled is True
    assert loaded.last_event == "continuation_scheduled:user"


def test_message_tool_turn_still_schedules_continuation(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = MessageToolAutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    async def no_progress(_: str) -> None:
        return None

    response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
            on_progress=no_progress,
        )
    )

    assert response is None
    assert bus.outbound_size == 1
    assert bus.inbound_size == 1

    outbound = asyncio.run(bus.consume_outbound())
    assert outbound.channel == "cli"
    assert outbound.chat_id == "direct"
    assert outbound.content == "已通过 message 工具发送当前步骤结果。"

    scheduled = asyncio.run(bus.consume_inbound())
    assert scheduled.channel == "system"
    assert scheduled.sender_id == "self"
    assert scheduled.metadata["_task_continue"] is True
    assert scheduled.metadata["_silent"] is True

    task_state = loop.task_states.load_active("cli:direct")
    assert task_state is not None
    assert scheduled.metadata["_task_id"] == task_state.task_id
    assert task_state.phase == "executing"
    assert task_state.current_step_index == 2
    assert task_state.continuation_scheduled is True


def test_autorun_continuation_chain_runs_to_completion(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SequencedAutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    first_response = asyncio.run(
        loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="请帮我实现任务状态"),
            session_key="cli:direct",
        )
    )

    assert first_response is None
    assert bus.inbound_size == 1

    second_turn = asyncio.run(bus.consume_inbound())
    second_response = asyncio.run(loop._process_message(second_turn))
    assert second_response is None
    assert bus.inbound_size == 1

    third_turn = asyncio.run(bus.consume_inbound())
    final_response = asyncio.run(loop._process_message(third_turn))

    assert final_response is not None
    assert final_response.channel == "cli"
    assert final_response.chat_id == "direct"
    assert final_response.content == "已完成当前步骤：步骤3。"
    assert provider.execution_count == 3
    assert bus.inbound_size == 0
    assert loop.task_states.load_active("cli:direct") is None


def test_autorun_continuation_chain_returns_final_outbound_for_feishu(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = SequencedAutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    first_response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="user",
                chat_id="ou_user_1",
                content="请帮我实现任务状态",
            ),
            session_key="feishu:ou_user_1",
        )
    )

    assert first_response is None
    second_turn = asyncio.run(bus.consume_inbound())
    second_response = asyncio.run(loop._process_message(second_turn))
    assert second_response is None

    third_turn = asyncio.run(bus.consume_inbound())
    final_response = asyncio.run(loop._process_message(third_turn))

    assert final_response is not None
    assert final_response.channel == "feishu"
    assert final_response.chat_id == "ou_user_1"
    assert final_response.content == "已完成当前步骤：步骤3。"
    assert loop.task_states.load_active("feishu:ou_user_1") is None


def test_subagent_result_resumes_waiting_step_and_autoruns(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = loop.task_states.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.steps = build_steps(["分析需求", "实现状态", "补测试"])
    task.steps[0].status = "waiting"
    task.steps[0].executor = "subagent"
    task.steps[0].spawn_task_id = "sg-001"
    task.steps[1].status = "pending"
    task.steps[2].status = "pending"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    sync_task_pointers(task)
    loop.task_states.save_active(task)

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:direct",
                content="后台任务已完成：产出实现草稿。",
                metadata={
                    "_task_id": task.task_id,
                    "_subagent_result": True,
                    "_subagent_task_id": "sg-001",
                    "_subagent_status": "ok",
                    "_silent": True,
                    "_session_key": "cli:direct",
                },
            )
        )
    )

    assert response is None
    loaded = loop.task_states.load_active("cli:direct")
    assert loaded is not None
    assert loaded.phase == "executing"
    assert loaded.current_step_index == 2
    assert loaded.pending_subagent_ids == []
    assert loaded.waiting_reason == ""
    assert loaded.continuation_scheduled is True
    assert loaded.last_event == "continuation_scheduled:subagent"
    assert loaded.steps[0].status == "done"
    assert loaded.steps[0].spawn_task_id == ""
    assert loaded.steps[0].notes == "后台任务已完成：产出实现草稿。"
    assert "subagent_result:sg-001" in loaded.steps[0].evidence
    assert bus.inbound_size == 1

    scheduled = asyncio.run(bus.consume_inbound())
    assert scheduled.metadata["_task_continue"] is True
    assert scheduled.metadata["_task_id"] == task.task_id


def test_completed_dirty_active_task_is_archived_before_new_user_turn(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = loop.task_states.create_new_task("feishu:direct", "旧任务")
    task.phase = "waiting_subagent"
    task.status = "active"
    task.steps = build_steps(["分析需求", "实现状态"])
    for step in task.steps:
        step.status = "done"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    sync_task_pointers(task)
    loop.task_states.save_active(task)

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="user",
                chat_id="direct",
                content="请帮我实现任务状态",
            ),
            session_key="feishu:direct",
        )
    )

    assert response is None
    archived = list((workspace / "task_state" / "archive").glob("feishu_direct__*.json"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8").find('"phase": "completed"') != -1

    loaded = loop.task_states.load_active("feishu:direct")
    assert loaded is not None
    assert loaded.phase == "executing"
    assert loaded.current_step_index == 2
    assert bus.inbound_size == 1


def test_completed_dirty_active_task_ignores_stale_subagent_result(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    provider = AutorunProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = loop.task_states.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.status = "active"
    task.steps = build_steps(["分析需求", "实现状态"])
    for step in task.steps:
        step.status = "done"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    sync_task_pointers(task)
    loop.task_states.save_active(task)

    response = asyncio.run(
        loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:direct",
                content="后台任务结果",
                metadata={
                    "_task_id": task.task_id,
                    "_subagent_result": True,
                    "_subagent_task_id": "sg-001",
                    "_subagent_status": "ok",
                    "_silent": True,
                    "_session_key": "cli:direct",
                },
            )
        )
    )

    assert response is None
    assert loop.task_states.load_active("cli:direct") is None
    archived = list((workspace / "task_state" / "archive").glob("cli_direct__*.json"))
    assert len(archived) == 1


def test_subagent_timeout_result_is_reported_as_error(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    manager = SubagentManager(
        provider=SubagentErrorProvider(),
        workspace=workspace,
        bus=bus,
    )

    asyncio.run(
        manager._run_subagent(
            "sg-001",
            "实现当前步骤",
            "实现当前步骤",
            {"channel": "cli", "chat_id": "direct"},
        )
    )

    assert bus.inbound_size == 1
    announced = asyncio.run(bus.consume_inbound())
    assert announced.metadata["_subagent_result"] is True
    assert announced.metadata["_subagent_status"] == "error"
    assert "Error: request timed out after 30.0s" in announced.content


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [
        (
            "任务已完成。此前日志里出现过 request timed out after 30.0s，但重试后成功。",
            "ok",
        ),
        (
            '任务已完成。原始日志包含 "Error: request timed out after 30.0s"，这里只是引用。',
            "ok",
        ),
    ],
)
def test_subagent_reference_to_error_text_is_not_reported_as_failure(
    tmp_path: Path,
    content: str,
    expected_status: str,
) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    manager = SubagentManager(
        provider=StaticSubagentResultProvider(content),
        workspace=workspace,
        bus=bus,
    )

    asyncio.run(
        manager._run_subagent(
            "sg-001",
            "实现当前步骤",
            "实现当前步骤",
            {"channel": "cli", "chat_id": "direct"},
        )
    )

    assert bus.inbound_size == 1
    announced = asyncio.run(bus.consume_inbound())
    assert announced.metadata["_subagent_result"] is True
    assert announced.metadata["_subagent_status"] == expected_status
    assert content in announced.content


def test_subagent_provider_exception_is_reported_as_error(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    bus = MessageBus()
    manager = SubagentManager(
        provider=RaisingSubagentProvider(),
        workspace=workspace,
        bus=bus,
    )

    asyncio.run(
        manager._run_subagent(
            "sg-001",
            "实现当前步骤",
            "实现当前步骤",
            {"channel": "cli", "chat_id": "direct"},
        )
    )

    assert bus.inbound_size == 1
    announced = asyncio.run(bus.consume_inbound())
    assert announced.metadata["_subagent_result"] is True
    assert announced.metadata["_subagent_status"] == "error"
    assert "Error: provider crashed" in announced.content


def test_extract_spawn_result_supports_structured_payload() -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=Path("/tmp"),
        session_manager=SessionManager(Path("/tmp")),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    result = loop._maybe_extract_spawn_result(
        [
            {
                "name": "spawn",
                "result": {
                    "message": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-structured\nstatus: started",
                    "label": "实现当前步骤",
                    "id": "sg-structured",
                    "status": "started",
                },
            }
        ]
    )

    assert result == {
        "spawn_task_id": "sg-structured",
        "result": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-structured\nstatus: started",
    }


def test_extract_spawn_result_keeps_legacy_text_fallback(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    result = loop._maybe_extract_spawn_result(
        [
            {
                "name": "spawn",
                "result": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-legacy\nstatus: started",
            }
        ]
    )

    assert result == {
        "spawn_task_id": "sg-legacy",
        "result": "Subagent task accepted.\nlabel: 实现当前步骤\nid: sg-legacy\nstatus: started",
    }


def _make_executing_task(loop: AgentLoop, *, session_key: str = "cli:direct"):
    task = loop.task_states.create_new_task(session_key, "实现任务状态")
    task.phase = "executing"
    task.status = "active"
    task.steps = build_steps(["分析需求", "实现状态"])
    task.steps[0].status = "in_progress"
    sync_task_pointers(task)
    return task


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda loop, task: None, "ok"),
        (lambda loop, task: setattr(task, "phase", "waiting_subagent"), "phase=waiting_subagent"),
        (lambda loop, task: setattr(task, "status", "done"), "status=done"),
        (lambda loop, task: setattr(task, "continuation_scheduled", True), "already_scheduled"),
        (lambda loop, task: setattr(task.steps[0], "status", "waiting"), "no_active_step"),
    ],
)
def test_should_auto_continue_decision_reasons(tmp_path: Path, mutator, expected: str) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = _make_executing_task(loop)
    mutator(loop, task)
    allowed, reason = loop._should_auto_continue(task)

    assert (allowed, reason) == (expected == "ok", expected)


def test_should_auto_continue_handles_missing_task(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    assert loop._should_auto_continue(None) == (False, "no_task")


def test_should_auto_continue_blocks_when_auto_run_budget_exhausted(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = _make_executing_task(loop)
    task.auto_run_count = task.max_auto_runs

    allowed, reason = loop._should_auto_continue(task)

    assert (allowed, reason) == (False, "auto_run_budget_exhausted")
    loaded = loop.task_states.load_active("cli:direct")
    assert loaded is not None
    assert loaded.phase == "blocked"
    assert loaded.waiting_reason == "达到自动续跑上限，等待用户确认。"


def test_should_auto_continue_blocks_when_failure_budget_exhausted(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=AutorunProvider(),
        workspace=workspace,
        session_manager=SessionManager(workspace),
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = _make_executing_task(loop)
    task.failure_count = task.max_failures

    allowed, reason = loop._should_auto_continue(task)

    assert (allowed, reason) == (False, "failure_budget_exhausted")
    loaded = loop.task_states.load_active("cli:direct")
    assert loaded is not None
    assert loaded.phase == "blocked"
    assert loaded.waiting_reason == "连续失败次数过多，等待用户确认。"
