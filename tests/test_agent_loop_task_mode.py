import asyncio
from pathlib import Path

from nano_alice.agent.loop import AgentLoop
from nano_alice.bus.events import InboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.config.schema import MemoryAgentConfig
from nano_alice.providers.base import LLMProvider, LLMResponse
from nano_alice.session.manager import SessionManager
from nano_alice.agent.task_state import build_steps


class PlanningProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        system_text = messages[0]["content"]
        if "planning_mode" in system_text:
            return LLMResponse(
                content='{"summary":"任务状态实现","strategy":"先规划后执行","steps":["分析需求","实现状态存储","补测试"]}',
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


class MixedQuestionProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key=None, api_base=None)
        self.calls = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls.append(messages)
        return LLMResponse(
            content="这是 AI 心跳机制，不是人类心跳。",
            finish_reason="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        )

    def get_default_model(self) -> str:
        return "fake/model"


def test_process_direct_creates_task_state_and_injects_xml(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("long term note", encoding="utf-8")

    bus = MessageBus()
    provider = PlanningProvider()
    sessions = SessionManager(workspace)

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
                content="请帮我设计并实现任务状态机制",
                media=[],
                metadata={},
            ),
            session_key="cli:direct",
            on_progress=None,
            direct_return_required=True,
        )
    )

    assert response is not None
    assert response.metadata["mode"] == "task"
    assert response.metadata["task_phase"] == "executing"
    assert response.metadata["task_current_step_index"] == 2

    seen_system = provider.calls[-1][0]["content"]
    assert "<task_execution_rules>" in seen_system
    assert "<task_state>" in seen_system
    assert "<current_step_index>1</current_step_index>" in seen_system

    archived = list((workspace / "task_state" / "archive").glob("*.json"))
    active = list((workspace / "task_state" / "active").glob("*.json"))
    assert not archived
    assert len(active) == 1


def test_mixed_question_stays_in_chat_mode_without_creating_task(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("stable identity", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("long term note", encoding="utf-8")

    bus = MessageBus()
    provider = MixedQuestionProvider()
    sessions = SessionManager(workspace)

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
                content="发我你的心跳文件附件，你知道你的心跳怎么运行吗",
                media=[],
                metadata={},
            ),
            session_key="cli:direct",
            on_progress=None,
            direct_return_required=True,
        )
    )

    assert response is not None
    assert response.metadata["mode"] == "chat"
    assert "task_phase" not in response.metadata
    assert all("planning_mode" not in call[0]["content"] for call in provider.calls)
    active = list((workspace / "task_state" / "active").glob("*.json"))
    assert not active


def test_plan_misalignment_detector_rejects_human_heartbeat_expansion(tmp_path: Path) -> None:
    workspace = tmp_path
    bus = MessageBus()
    provider = MixedQuestionProvider()
    sessions = SessionManager(workspace)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        session_manager=sessions,
        memory_agent_config=MemoryAgentConfig(enabled=False),
    )

    task = loop.task_states.create_new_task("cli:direct", "你知道你的心跳怎么运行吗")
    task.summary = "规划一个任务来创建并发送关于心跳运行机制的文件附件。"
    task.strategy = "先解释人类心跳机制，再整理成文件。"
    task.steps = build_steps(
        [
            "收集或生成关于人类心跳运行机制的准确信息，包括生理过程和相关知识。",
            "将心跳运行信息整理成结构化文本。",
        ]
    )

    assert loop._plan_looks_misaligned(task.goal, task) is True
