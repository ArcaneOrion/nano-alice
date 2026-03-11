"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nano_alice.agent.context import ContextBuilder
from nano_alice.agent.daily_cache import DailyCacheRecorder, DailyCacheStore, TodayRecallRetriever
from nano_alice.agent.memory import MemoryStore
from nano_alice.agent.reminder_intent import ReminderIntent, ReminderIntentStore
from nano_alice.agent.subagent import SubagentManager
from nano_alice.agent.task_state import (
    TaskRouteDecision,
    TaskRouter,
    TaskState,
    TaskStateRenderer,
    TaskStateStore,
    build_steps,
    extract_plan_from_text,
    normalize_task_state,
    sync_task_pointers,
)
from nano_alice.agent.tools.cron import CronTool
from nano_alice.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nano_alice.agent.tools.message import MessageTool
from nano_alice.agent.tools.registry import ToolRegistry
from nano_alice.agent.tools.shell import ExecTool
from nano_alice.agent.tools.spawn import SpawnTool
from nano_alice.agent.tools.web import WebFetchTool, make_search_tool
from nano_alice.bus.events import DeliveryReceipt, InboundMessage, OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.heartbeat.service import (
    HEARTBEAT_OK_TOKEN,
    heartbeat_response_preview,
    normalize_heartbeat_response,
    parse_heartbeat_decision,
)
from nano_alice.logging_utils import summarize_tool_result
from nano_alice.providers.base import LLMProvider, LLMResponse, preview_text, summarize_tool_calls
from nano_alice.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nano_alice.config.schema import EmbeddingsConfig, ExecToolConfig, MemoryAgentConfig
    from nano_alice.cron.service import CronService

_LLM_MAX_RETRIES = 3
_LLM_RETRY_BASE_DELAY = 2  # 秒，指数退避基数

# 匹配到这些关键词的错误不重试（认证/权限类，重试也没用）
_NON_RETRYABLE_PATTERNS = (
    "401",
    "403",
    "AuthenticationError",
    "PermissionDenied",
    "invalid api key",
    "invalid_api_key",
    "Unauthorized",
)

# Keywords indicating user explicitly wants something remembered
_MEMORY_HIGH_KEYWORDS = (
    "记住",
    "别忘了",
    "记下",
    "记一下",
    "记录一下",
    "remember",
    "don't forget",
    "note that",
    "keep in mind",
)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        tavily_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        embeddings_config: EmbeddingsConfig | None = None,
        memory_agent_config: MemoryAgentConfig | None = None,
        memory_provider: LLMProvider | None = None,
        subagent_provider: LLMProvider | None = None,
    ):
        from nano_alice.config.schema import ExecToolConfig

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.tavily_api_key = tavily_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.embeddings_config = embeddings_config

        self.context = ContextBuilder(workspace)
        self.daily_cache_store = DailyCacheStore(workspace)
        self.daily_cache_recorder = DailyCacheRecorder()
        self.today_recall = TodayRecallRetriever(self.daily_cache_store)
        self.sessions = session_manager or SessionManager(workspace)
        self.task_states = TaskStateStore(workspace)
        self.reminder_intents = ReminderIntentStore(workspace)
        self.task_router = TaskRouter()
        self.task_renderer = TaskStateRenderer()
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=subagent_provider or provider,
            workspace=workspace,
            bus=bus,
            model=subagent_provider.get_default_model() if subagent_provider else self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            tavily_api_key=tavily_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._pending_tasks: list[asyncio.Task] = []  # Background tasks (memory agent, etc.)

        # Memory agent config
        self._memory_agent_enabled = memory_agent_config.enabled if memory_agent_config else True
        self._memory_agent_model = (
            memory_agent_config.model if memory_agent_config and memory_agent_config.model else None
        ) or self.model
        self._memory_agent_interval = (
            memory_agent_config.interval
            if memory_agent_config and memory_agent_config.interval
            else 10
        )
        self._memory_provider = memory_provider or self.provider

        # RAG: shared memory index for recall
        self._memory_index = None
        if embeddings_config and embeddings_config.api_key and embeddings_config.api_base:
            from nano_alice.agent.tools.memory_search import _MemoryIndex

            self._memory_index = _MemoryIndex(
                memory_dir=workspace / "memory",
                api_base=embeddings_config.api_base,
                api_key=embeddings_config.api_key,
                model=embeddings_config.model,
                dimensions=embeddings_config.dimensions,
                extra_headers=embeddings_config.extra_headers,
                min_score=embeddings_config.rag_min_score,
            )

        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
            )
        )
        self.tools.register(
            make_search_tool(
                api_key=self.brave_api_key,
                tavily_api_key=self.tavily_api_key,
            )
        )
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        if (
            self.embeddings_config
            and self.embeddings_config.api_key
            and self.embeddings_config.api_base
        ):
            from nano_alice.agent.tools.memory_search import MemorySearchTool

            self.tools.register(
                MemorySearchTool(
                    workspace=self.workspace,
                    api_base=self.embeddings_config.api_base,
                    api_key=self.embeddings_config.api_key,
                    model=self.embeddings_config.model,
                    dimensions=self.embeddings_config.dimensions,
                    extra_headers=self.embeddings_config.extra_headers,
                )
            )

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nano_alice.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.set_context(channel, chat_id, message_id)

        if spawn_tool := self.tools.get("spawn"):
            if isinstance(spawn_tool, SpawnTool):
                spawn_tool.set_context(channel, chat_id)

        if cron_tool := self.tools.get("cron"):
            if isinstance(cron_tool, CronTool):
                cron_tool.set_context(channel, chat_id)

    @staticmethod
    def _parse_origin_chat(chat_id: str) -> tuple[str, str]:
        if ":" in chat_id:
            return chat_id.split(":", 1)
        return "cli", chat_id

    def _build_intent_due_message(self, intent: ReminderIntent) -> str:
        notify_policy = json.dumps(intent.notify_policy, ensure_ascii=False)
        last_delivery = json.dumps(intent.delivery_state.__dict__, ensure_ascii=False)
        return (
            "[Internal Reminder Intent Due]\n"
            "This is your own internal reminder intent becoming due. "
            "It is not a new user message.\n\n"
            f"Intent ID: {intent.intent_id}\n"
            f"Origin Session: {intent.session_key}\n"
            f"Goal: {intent.goal}\n"
            f"Why Notify: {intent.why_notify}\n"
            f"Notify Policy: {notify_policy}\n"
            f"Last Delivery State: {last_delivery}\n\n"
            "Decide whether to notify the user now. "
            "If yes, write the exact user-facing message. "
            "If no, return an empty response."
        )

    def _record_delivery_receipt(self, receipt: DeliveryReceipt) -> None:
        if receipt.intent_id:
            status = receipt.status if receipt.status in {"pending", "sent", "failed", "skipped"} else "failed"
            error = receipt.error
            if status == "failed" and receipt.status not in {"failed", "pending", "sent", "skipped"}:
                error = error or f"unknown receipt status: {receipt.status}"
            self.reminder_intents.update_delivery(
                receipt.intent_id,
                status=status,
                message_id=receipt.provider_message_id,
                error=error,
                delivered_at=receipt.delivered_at,
            )

        if not receipt.session_key:
            return

        session = self.sessions.get_or_create(receipt.session_key)
        session.metadata["last_delivery_receipt"] = {
            "channel": receipt.channel,
            "chat_id": receipt.chat_id,
            "status": receipt.status,
            "provider_message_id": receipt.provider_message_id,
            "error": receipt.error,
            "intent_id": receipt.intent_id,
            "delivered_at": receipt.delivered_at,
            "content_preview": receipt.content_preview,
        }
        self.sessions.save(session)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    def _build_task_goal(self, msg: InboundMessage, active_task: TaskState | None) -> str:
        if active_task and active_task.goal:
            return msg.content.strip() or active_task.goal
        return msg.content.strip()

    @staticmethod
    def _get_current_step(task_state: TaskState | None):
        if task_state is None:
            return None
        return next(
            (step for step in task_state.steps if step.index == task_state.current_step_index),
            None,
        )

    @staticmethod
    def _get_next_step(task_state: TaskState, current_index: int):
        return next((step for step in task_state.steps if step.index == current_index + 1), None)

    def _normalize_task_state(
        self,
        task_state: TaskState | None,
        *,
        archive_completed: bool = False,
        context: str = "runtime",
    ) -> TaskState | None:
        if task_state is None:
            return None
        task_state, changed, reasons = normalize_task_state(task_state)
        if changed:
            logger.warning(
                "Normalized task state: task_id={} context={} phase={} status={} reasons={}",
                task_state.task_id,
                context,
                task_state.phase,
                task_state.status,
                ",".join(reasons) or "-",
            )
            self.task_states.save_active(task_state)
        if archive_completed and task_state.phase == "completed":
            self.task_states.archive_active(task_state.session_key)
            logger.info(
                "Archived completed task during normalization: task_id={} context={}",
                task_state.task_id,
                context,
            )
            return None
        return task_state

    def _mark_step_waiting_subagent(
        self,
        task_state: TaskState | None,
        spawn_task_id: str,
        note: str,
    ) -> TaskState | None:
        current = self._get_current_step(task_state)
        if task_state is None or current is None:
            return task_state
        current.status = "waiting"
        current.executor = "subagent"
        current.spawn_task_id = spawn_task_id
        current.notes = note[:500]
        if note:
            current.evidence.append(f"spawn:{spawn_task_id}")
        task_state.phase = "waiting_subagent"
        task_state.waiting_reason = f"waiting for subagent {spawn_task_id}"
        task_state.last_action = f"delegated step {current.index}: {current.title}"
        task_state.last_event = "step_waiting_subagent"
        task_state.continuation_scheduled = False
        if spawn_task_id not in task_state.pending_subagent_ids:
            task_state.pending_subagent_ids.append(spawn_task_id)
        sync_task_pointers(task_state)
        self.task_states.save_active(task_state)
        return task_state

    def _mark_task_blocked(self, task_state: TaskState | None, reason: str) -> TaskState | None:
        if task_state is None:
            return None
        current = self._get_current_step(task_state)
        if current is not None and current.status not in {"done", "failed"}:
            current.status = "blocked"
            current.notes = reason[:500]
        task_state.phase = "blocked"
        task_state.waiting_reason = reason[:500]
        task_state.last_event = "task_blocked"
        task_state.continuation_scheduled = False
        sync_task_pointers(task_state)
        self.task_states.save_active(task_state)
        return task_state

    def _resume_waiting_step_from_subagent(
        self,
        task_state: TaskState | None,
        spawn_task_id: str,
        result_text: str,
        status: str,
    ) -> tuple[TaskState | None, str | None]:
        if task_state is None:
            return None, None
        current = next(
            (
                step
                for step in task_state.steps
                if step.status == "waiting" and step.spawn_task_id == spawn_task_id
            ),
            None,
        )
        if current is None:
            logger.info(
                "Ignoring stale subagent result: task_id={} spawn_task_id={}",
                task_state.task_id,
                spawn_task_id,
            )
            return None, None
        if spawn_task_id in task_state.pending_subagent_ids:
            task_state.pending_subagent_ids.remove(spawn_task_id)
        task_state.waiting_reason = ""
        task_state.continuation_scheduled = False
        if status == "error":
            task_state.failure_count += 1
            current.status = "failed"
            current.notes = result_text[:500]
            current.evidence.append(f"subagent_error:{spawn_task_id}")
            task_state.last_event = "subagent_failed"
            self._mark_task_blocked(task_state, f"Subagent {spawn_task_id} failed: {result_text[:200]}")
            return task_state, f"任务已暂停：后台步骤失败。{result_text[:200]}"
        current.status = "in_progress"
        current.notes = result_text[:500]
        current.evidence.append(f"subagent_result:{spawn_task_id}")
        task_state.phase = "executing"
        task_state.last_action = f"received subagent result for step {current.index}: {current.title}"
        task_state.last_event = "subagent_result_received"
        sync_task_pointers(task_state)
        self.task_states.save_active(task_state)
        return task_state, None

    def _maybe_extract_spawn_result(self, tool_events: list[dict[str, Any]]) -> dict[str, str] | None:
        for event in reversed(tool_events):
            if event.get("name") != "spawn":
                continue
            raw_result = event.get("result")
            structured = raw_result if isinstance(raw_result, dict) else None
            if structured is None and isinstance(raw_result, str):
                try:
                    parsed = json.loads(raw_result)
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    structured = parsed
            if structured is not None:
                spawn_task_id = str(structured.get("spawn_task_id") or structured.get("id") or "").strip()
                if spawn_task_id:
                    message = str(structured.get("message") or raw_result or "")
                    return {
                        "spawn_task_id": spawn_task_id,
                        "result": message,
                    }
            result = str(raw_result or "")
            match = re.search(r"id:\s*([A-Za-z0-9_-]+)", result)
            if not match:
                logger.debug(
                    "Spawn result missing task id: preview={}",
                    result[:200] if result else "-",
                )
                continue
            return {
                "spawn_task_id": match.group(1),
                "result": result,
            }
        return None

    def _should_auto_continue(self, task_state: TaskState | None) -> tuple[bool, str]:
        if task_state is None:
            return False, "no_task"
        if task_state.phase != "executing":
            return False, f"phase={task_state.phase}"
        if task_state.status != "active":
            return False, f"status={task_state.status}"
        if task_state.continuation_scheduled:
            return False, "already_scheduled"
        if task_state.auto_run_count >= task_state.max_auto_runs:
            self._mark_task_blocked(task_state, "达到自动续跑上限，等待用户确认。")
            return False, "auto_run_budget_exhausted"
        if task_state.failure_count >= task_state.max_failures:
            self._mark_task_blocked(task_state, "连续失败次数过多，等待用户确认。")
            return False, "failure_budget_exhausted"
        current = self._get_current_step(task_state)
        if current is None or current.status != "in_progress":
            return False, "no_active_step"
        return True, "ok"

    async def _publish_task_continuation(
        self,
        task_state: TaskState | None,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        reason: str,
    ) -> bool:
        allowed, decision = self._should_auto_continue(task_state)
        if not allowed or task_state is None:
            logger.info(
                "Continuation skipped: task_id={} reason={}",
                task_state.task_id if task_state else "-",
                decision,
            )
            return False
        task_state.continuation_scheduled = True
        task_state.auto_run_count += 1
        task_state.last_event = f"continuation_scheduled:{reason}"
        self.task_states.save_active(task_state)
        await self.bus.publish_inbound(
            InboundMessage(
                channel="system",
                sender_id="self",
                chat_id=f"{channel}:{chat_id}",
                content="Continue active task using current task_state. Execute only the current allowed step.",
                metadata={
                    "_session_key": session_key,
                    "_task_continue": True,
                    "_task_id": task_state.task_id,
                    "_silent": True,
                },
            )
        )
        logger.info("Continuation scheduled: task_id={} reason={}", task_state.task_id, reason)
        return True

    async def _plan_task(
        self, session: Session, msg: InboundMessage, task_state: TaskState
    ) -> TaskState:
        planner_prompt = (
            "你处于任务规划阶段。请只输出 JSON，不要输出额外解释。\n"
            '输出格式: {"summary": "...", "strategy": "...", "steps": ["步骤1", "步骤2"]}\n'
            "要求:\n"
            "1. steps 必须是 3-7 个线性步骤\n"
            "2. 每个步骤必须可执行、不可跳步\n"
            "3. 不要声称已经完成任务\n"
            f"任务目标: {task_state.goal}"
        )
        planning_messages = [
            {
                "role": "system",
                "content": (
                    self.task_renderer.render_task_rules_xml()
                    + "\n<planning_mode>你现在只能规划，不能执行。</planning_mode>"
                ),
            },
            *session.get_history(max_messages=min(self.memory_window, 12)),
            {"role": "user", "content": planner_prompt},
        ]
        response = await self._chat_with_retry(planning_messages, tools=None)
        plan_lines = extract_plan_from_text(response.content or "")
        if not plan_lines:
            plan_lines = ["分析当前请求与约束", "设计执行方案", "执行当前第一步"]
        task_state.steps = build_steps(plan_lines[:7])
        task_state.summary = (response.content or task_state.goal).strip()[:200]
        if response.content:
            try:
                parsed = json.loads(re.search(r"\{[\s\S]*\}", response.content).group(0))
                if isinstance(parsed, dict):
                    task_state.summary = str(parsed.get("summary") or task_state.summary)[:200]
                    task_state.strategy = str(parsed.get("strategy") or task_state.strategy)[:200]
            except Exception:
                pass
        task_state.phase = "executing"
        task_state.waiting_reason = ""
        task_state.continuation_scheduled = False
        task_state.last_event = "task_planned"
        sync_task_pointers(task_state)
        self.task_states.save_active(task_state)
        return task_state

    def _maybe_start_task(
        self,
        session_key: str,
        msg: InboundMessage,
        active_task: TaskState | None,
        route: TaskRouteDecision,
    ) -> TaskState | None:
        if route.mode != "task":
            return None
        if active_task is None:
            task_state = self.task_states.create_new_task(
                session_key=session_key,
                goal=self._build_task_goal(msg, active_task),
                summary=msg.content,
            )
            task_state.last_event = "task_created"
            self.task_states.save_active(task_state)
            return task_state
        if route.needs_replan:
            active_task.goal = msg.content.strip() or active_task.goal
            active_task.phase = "replanning"
            active_task.plan_version += 1
            active_task.continuation_scheduled = False
            active_task.last_action = "user requested replanning"
            active_task.last_event = "task_replanned"
            self.task_states.save_active(active_task)
        return active_task

    def _complete_current_step(
        self,
        task_state: TaskState | None,
        final_content: str | None,
        tool_evidence: list[str],
    ) -> TaskState | None:
        if task_state is None or task_state.phase != "executing":
            return task_state
        current = self._get_current_step(task_state)
        if current is None:
            return task_state
        result_text = (final_content or "").strip()
        if not result_text and not tool_evidence:
            return task_state
        current.result = result_text[:500]
        current.evidence.extend(tool_evidence)
        current.status = "done"
        current.spawn_task_id = ""
        task_state.last_action = f"completed step {current.index}: {current.title}"
        task_state.last_event = "step_completed"
        task_state.waiting_reason = ""
        task_state.continuation_scheduled = False
        next_step = self._get_next_step(task_state, current.index)
        if next_step is None:
            task_state.phase = "completed"
            task_state.status = "done"
            task_state.current_step_index = current.index
            task_state.current_step_id = current.id
            task_state.next_step_id = ""
            self.task_states.save_active(task_state)
            self.task_states.archive_active(task_state.session_key)
            return None
        next_step.status = "in_progress"
        task_state.phase = "executing"
        task_state.last_event = "step_advanced"
        sync_task_pointers(task_state)
        self.task_states.save_active(task_state)
        return task_state

    async def _chat_with_retry(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """调用 LLM，遇到可重试错误自动退避重试。"""
        response: LLMResponse | None = None
        for attempt in range(_LLM_MAX_RETRIES + 1):  # 0..MAX_RETRIES
            t0 = time.perf_counter()
            response = await self.provider.chat(
                messages=messages,
                tools=tools,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            elapsed = time.perf_counter() - t0

            # 正常返回
            if response.finish_reason != "error":
                usage = response.usage or {}
                provider_meta = response.provider_metadata or {}
                logger.info(
                    "LLM call: request_id={} model={} resolved_model={} provider={} endpoint={} trace={} | {:.1f}s | prompt={} compl={} total={} | finish_reason={} | tool_calls={} | preview={}",
                    provider_meta.get("request_id", "-"),
                    self.model,
                    provider_meta.get("resolved_model", self.model),
                    provider_meta.get("provider_name", type(self.provider).__name__),
                    provider_meta.get("endpoint", "-"),
                    provider_meta.get("trace_path", "-"),
                    elapsed,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("total_tokens", 0),
                    response.finish_reason,
                    summarize_tool_calls(response.tool_calls),
                    preview_text(response.content),
                )
                logger.debug(
                    "LLM call details: provider_meta={} content={} reasoning={}",
                    provider_meta,
                    response.content or "",
                    response.reasoning_content or "",
                )
                return response

            error_text = response.content or ""

            # 不可重试的错误（认证/权限），立即返回
            if any(p in error_text for p in _NON_RETRYABLE_PATTERNS):
                logger.error("LLM non-retryable error: {}", error_text[:200])
                return response

            # 最后一次尝试也失败，返回错误
            if attempt == _LLM_MAX_RETRIES:
                logger.error("LLM failed after {} retries: {}", _LLM_MAX_RETRIES, error_text[:200])
                return response

            # 可重试：指数退避
            delay = _LLM_RETRY_BASE_DELAY * (2**attempt)  # 2s, 4s, 8s
            logger.warning(
                "LLM error (attempt {}/{}), retrying in {}s: {}",
                attempt + 1,
                _LLM_MAX_RETRIES + 1,
                delay,
                error_text[:200],
            )
            await asyncio.sleep(delay)

        return response  # type: ignore[return-value]  # unreachable, for type checker

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        *,
        stop_on_spawn: bool = False,
    ) -> tuple[str | None, list[str], dict[str, int], list[str], list[dict[str, Any]]]:
        """Run the agent iteration loop. Returns final content, tool names, usage, evidence, tool events."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        evidence: list[str] = []
        tool_events: list[dict[str, Any]] = []
        total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        loop_t0 = time.perf_counter()

        while iteration < self.max_iterations:
            iteration += 1

            response = await self._chat_with_retry(messages, self.tools.get_definitions())

            # Accumulate token usage
            if response.usage:
                for k in total_usage:
                    total_usage[k] += response.usage.get(k, 0)

            if response.has_tool_calls:
                stop_after_spawn = False
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls))

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    result_summary = summarize_tool_result(tool_call.name, result)
                    logger.info(
                        "Tool result: {} bytes={} kind={} preview={}",
                        tool_call.name,
                        result_summary["result_bytes"],
                        result_summary["result_kind"],
                        result_summary["preview"],
                    )
                    result_preview = (
                        result
                        if isinstance(result, str)
                        else json.dumps(result, ensure_ascii=False)
                    )
                    tool_events.append(
                        {
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "result": result_preview,
                        }
                    )
                    evidence.append(f"tool:{tool_call.name}")
                    evidence.append(f"result:{result_preview[:120]}")
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                    if stop_on_spawn and tool_call.name == "spawn":
                        stop_after_spawn = True
                        final_content = self._strip_think(response.content)
                        break
                if stop_after_spawn:
                    break
            else:
                final_content = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error(
                        "LLM returned error after retries: {}", (final_content or "")[:200]
                    )
                break

        loop_elapsed = time.perf_counter() - loop_t0
        logger.info(
            "Agent loop done: {} iterations | {:.1f}s | prompt={} compl={} total={}",
            iteration,
            loop_elapsed,
            total_usage["prompt_tokens"],
            total_usage["completion_tokens"],
            total_usage["total_tokens"],
        )
        return final_content, tools_used, total_usage, evidence, tool_events

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                try:
                    sk = msg.metadata.get("_session_key")
                    response = await self._process_message(msg, session_key=sk)
                    if response is not None:
                        if sk == "heartbeat":
                            _, normalized = normalize_heartbeat_response(response.content)
                            response.content = normalized

                        if (
                            sk == "heartbeat"
                            and (response.content or "").strip() == HEARTBEAT_OK_TOKEN
                        ):
                            logger.info("Heartbeat: OK (no action needed)")
                        else:
                            await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=msg.metadata or {},
                            )
                        )
                except Exception as e:
                    logger.error("Error processing message: {}", e)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"Sorry, I encountered an error: {str(e)}",
                        )
                    )
            except asyncio.TimeoutError:
                continue

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    async def await_pending(self) -> None:
        """Wait for background tasks (memory agent, etc.) to complete."""
        tasks = [t for t in self._pending_tasks if not t.done()]
        self._pending_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        direct_return_required: bool = False,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        internal_task_event = msg.channel == "system" and (
            msg.metadata.get("_task_continue") or msg.metadata.get("_subagent_result")
        )
        silent_internal = bool(msg.metadata.get("_silent"))

        if msg.channel == "system" and msg.metadata.get("_delivery_receipt"):
            receipt_data = msg.metadata.get("receipt") or {}
            try:
                receipt = DeliveryReceipt(**receipt_data)
            except TypeError:
                logger.warning("Ignoring malformed delivery receipt: {}", receipt_data)
                return None
            self._record_delivery_receipt(receipt)
            logger.info(
                "Processed delivery receipt: channel={} chat_id={} status={} intent_id={}",
                receipt.channel,
                receipt.chat_id,
                receipt.status,
                receipt.intent_id or "-",
            )
            return None

        if msg.channel == "system" and msg.metadata.get("_cron_intent_due"):
            origin_channel, origin_chat_id = self._parse_origin_chat(msg.chat_id)
            intent_id = str(msg.metadata.get("_intent_id") or "")
            intent = self.reminder_intents.load(intent_id)
            if intent is None:
                logger.warning("Ignoring due intent event without stored intent: {}", intent_id or "-")
                return None

            key = session_key or msg.metadata.get("_session_key") or intent.session_key
            session = self.sessions.get_or_create(key)
            self._set_tool_context(origin_channel, origin_chat_id, msg.metadata.get("message_id"))

            envelope = self.context.build_prompt_envelope(
                history=session.get_history(max_messages=self.memory_window),
                current_message=self._build_intent_due_message(intent),
                channel=origin_channel,
                chat_id=origin_chat_id,
                today_recall=None,
                task_rules_xml=self.task_renderer.render_task_rules_xml(),
            )
            messages = self.context.render_messages(envelope)
            try:
                final_content, _, token_usage, _, _ = await self._run_agent_loop(messages)
            except Exception as e:
                logger.error("Failed to process due reminder intent {}: {}", intent.intent_id, e)
                self.reminder_intents.update_delivery(
                    intent.intent_id,
                    status="failed",
                    error=str(e)[:500],
                )
                return None
            final_content = (final_content or "").strip()
            self.reminder_intents.mark_notified(intent.intent_id)
            if not final_content:
                self.reminder_intents.update_delivery(intent.intent_id, status="skipped")
                return None

            metadata = {
                "_intent_id": intent.intent_id,
                "_session_key": key,
            }
            if token_usage and any(v > 0 for v in token_usage.values()):
                metadata["token_usage"] = token_usage
            return OutboundMessage(
                channel=origin_channel,
                chat_id=origin_chat_id,
                content=final_content,
                metadata=metadata,
            )

        if internal_task_event:
            channel, chat_id = self._parse_origin_chat(msg.chat_id)
            msg = InboundMessage(
                channel=channel,
                sender_id=msg.sender_id,
                chat_id=chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
                media=msg.media,
                metadata=dict(msg.metadata or {}),
            )
            session_key = session_key or msg.metadata.get("_session_key") or msg.session_key

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = self._parse_origin_chat(msg.chat_id)
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            envelope = self.context.build_prompt_envelope(
                history=session.get_history(max_messages=self.memory_window),
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                today_recall=None,
                task_rules_xml=self.task_renderer.render_task_rules_xml(),
            )
            messages = self.context.render_messages(envelope)
            final_content, _, _, _, _ = await self._run_agent_loop(messages)
            session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
            session.add_message("assistant", final_content or "Background task completed.")
            self.sessions.save(session)
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            messages_to_archive = session.messages.copy()
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            self.task_states.archive_active(session.key)

            async def _consolidate_and_cleanup():
                temp = Session(key=session.key)
                temp.messages = messages_to_archive
                await self._consolidate_memory(temp, archive_all=True)

            asyncio.create_task(_consolidate_and_cleanup())
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="New session started. Memory consolidation in progress.",
            )
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "🐈 nano-alice commands:\n"
                    "/new — Start a new conversation\n"
                    "/memory — Reconcile existing memory files\n"
                    "/help — Show available commands"
                ),
            )
        if cmd == "/memory":
            result = await self.run_memory_maintenance()
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._format_memory_maintenance_result(result),
            )

        if len(session.messages) > self.memory_window and session.key not in self._consolidating:
            self._consolidating.add(session.key)

            async def _consolidate_and_unlock():
                try:
                    await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)

            asyncio.create_task(_consolidate_and_unlock())

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        active_task = self.task_states.load_active(session.key)
        active_task = self._normalize_task_state(active_task, context="load_active")
        if active_task is not None and active_task.phase == "completed":
            self.task_states.archive_active(session.key)
            if internal_task_event:
                logger.info("Ignoring internal task event for completed task: {}", active_task.task_id)
                return None
            active_task = None
        if internal_task_event and active_task is None:
            logger.info("Ignoring internal task event without active task: {}", msg.sender_id)
            return None
        if msg.metadata.get("_task_continue") and active_task is not None:
            continuation_task_id = str(msg.metadata.get("_task_id") or "")
            if not continuation_task_id or continuation_task_id != active_task.task_id:
                logger.info(
                    "Ignoring stale continuation: expected_task_id={} active_task_id={}",
                    continuation_task_id or "-",
                    active_task.task_id,
                )
                return None
            active_task.continuation_scheduled = False
            active_task.last_event = "continuation_resumed"
            self.task_states.save_active(active_task)
        forced_response: str | None = None
        if msg.metadata.get("_subagent_result"):
            active_task, forced_response = self._resume_waiting_step_from_subagent(
                active_task,
                str(msg.metadata.get("_subagent_task_id") or ""),
                msg.content,
                str(msg.metadata.get("_subagent_status") or "ok"),
            )
            if active_task is None and forced_response is None:
                return None
            active_task = self._normalize_task_state(active_task, context="post_subagent_result")
        route = self.task_router.decide(msg.content, active_task=active_task)
        task_state = self._maybe_start_task(session.key, msg, active_task, route)
        if task_state and task_state.phase in {"planning", "replanning"}:
            task_state = await self._plan_task(session, msg, task_state)
        if forced_response and task_state is not None and task_state.phase == "blocked":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=forced_response,
                metadata={"mode": "task", "task_phase": task_state.phase},
            )

        task_rules_xml = self.task_renderer.render_task_rules_xml()
        task_state_xml = self.task_renderer.render_task_state_xml(task_state)

        # RAG: semantic search for relevant memory
        recalled_context = None
        today_recall = None
        results = None
        if self._memory_index:
            try:
                results = await self._memory_index.search(msg.content, top_k=3)
                if results:
                    recalled_context = "\n\n---\n\n".join(
                        f"[{r['file']} L{r['lines']}] {r['text']}" for r in results
                    )
            except Exception as e:
                logger.warning("RAG recall failed: {}", e)
        try:
            today_recall = self.today_recall.recall(msg.content, session_key=session.key)
        except Exception as e:
            logger.warning("Today recall failed: {}", e)

        envelope = self.context.build_prompt_envelope(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            recalled_context=recalled_context,
            today_recall=today_recall,
            task_rules_xml=task_rules_xml,
            task_state_xml=task_state_xml,
        )
        initial_messages = self.context.render_messages(envelope)
        context_metrics = self.context.compute_context_metrics(envelope, initial_messages)
        logger.debug(
            "context built: system={} history={} current={} input={} history_messages={}",
            context_metrics["system_chars"],
            context_metrics["history_chars"],
            context_metrics["current_context_chars"],
            context_metrics["user_input_chars"],
            context_metrics["history_message_count"],
        )

        async def _bus_progress(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        effective_progress = None if silent_internal else (on_progress or _bus_progress)
        final_content, tools_used, token_usage, task_evidence, tool_events = await self._run_agent_loop(
            initial_messages,
            on_progress=effective_progress,
            stop_on_spawn=bool(task_state is not None and route.mode == "task"),
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if session_key == "heartbeat":
            raw_preview = heartbeat_response_preview(final_content)
            if raw_preview:
                logger.info("Heartbeat raw response: {}", raw_preview)

            if decision := parse_heartbeat_decision(final_content):
                logger.info(
                    "Heartbeat decision: should_push={}, reason={}, content_preview={}",
                    decision.should_push,
                    decision.reason or "-",
                    heartbeat_response_preview(decision.content),
                )

            _, final_content = normalize_heartbeat_response(final_content)

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        message_tool_sent = False
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                message_tool_sent = True
                final_content = message_tool._last_sent_content or final_content

        step_before = self._get_current_step(task_state)
        previous_step_index = step_before.index if step_before is not None else None
        spawn_info = self._maybe_extract_spawn_result(tool_events)
        step_finished = False
        if spawn_info is not None:
            task_state = self._mark_step_waiting_subagent(
                task_state,
                spawn_info["spawn_task_id"],
                spawn_info["result"],
            )
        else:
            task_state = self._complete_current_step(task_state, final_content, task_evidence)
            if previous_step_index is not None:
                step_finished = task_state is None or task_state.current_step_index != previous_step_index
        task_state = self._normalize_task_state(task_state, context="post_step")

        if not internal_task_event:
            session.add_message("user", msg.content, media=msg.media if msg.media else None)
            session.add_message(
                "assistant", final_content, tools_used=tools_used if tools_used else None
            )
            self.sessions.save(session)

        # Launch memory subagent in background (skip system/heartbeat/cron messages)
        is_heartbeat = session_key == "heartbeat" or msg.content.startswith("Read HEARTBEAT.md")
        is_cron = msg.sender_id == "cron"
        if (
            self._memory_agent_enabled
            and msg.channel != "system"
            and not internal_task_event
            and not is_heartbeat
            and not is_cron
        ):
            # Detect explicit memory keywords for priority signal
            msg_lower = msg.content.lower()
            memory_priority = (
                "high" if any(kw in msg_lower for kw in _MEMORY_HIGH_KEYWORDS) else "normal"
            )
            # 智能触发：判断是否需要运行 memory agent
            if self._should_run_memory_agent(msg.content, session, memory_priority):
                task = asyncio.create_task(
                    self._run_memory_agent(
                        session,
                        memory_priority=memory_priority,
                        pre_search_results=results,  # 传递 RAG 结果避免重复 embedding
                    )
                )
                self._pending_tasks.append(task)

        if self._should_record_daily_cache(
            msg=msg,
            session_key=session_key,
            internal_task_event=internal_task_event,
            tool_events=tool_events,
        ):
            task = asyncio.create_task(
                self._record_daily_cache(
                    session_key=session.key,
                    trigger=msg.content,
                    tool_events=tool_events,
                    final_content=final_content,
                )
            )
            self._pending_tasks.append(task)

        meta = dict(msg.metadata or {})
        meta["context_size"] = context_metrics
        meta["mode"] = route.mode
        if task_state:
            meta["task_phase"] = task_state.phase
            meta["task_current_step_index"] = task_state.current_step_index
            meta["task_current_step_id"] = task_state.current_step_id
        if token_usage and any(v > 0 for v in token_usage.values()):
            meta["token_usage"] = token_usage

        continuation_scheduled = False
        if step_finished and task_state and task_state.phase == "executing":
            continuation_scheduled = await self._publish_task_continuation(
                task_state,
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_key=session.key,
                reason=msg.sender_id,
            )
            if continuation_scheduled:
                meta["continuation_scheduled"] = True

        if task_state is not None and task_state.phase == "completed":
            self.task_states.archive_active(task_state.session_key)

        # Message tool was used to send the response to the user via bus.
        # For bus-driven channels (Telegram, Discord, etc.), the message is already sent
        # so we return None. For direct callers (CLI / cron), they don't consume the bus,
        # so we need to return the content directly.
        if message_tool_sent:
            if task_state is not None and task_state.phase == "waiting_subagent":
                if direct_return_required:
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="已委派后台任务，等待结果。",
                        metadata=meta,
                    )
                return None
            if direct_return_required and final_content:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=final_content,
                    metadata=meta,
                )
            return None

        # Bus-driven chat channels can stay quiet during autorun so users only see the
        # final stitched reply. Direct callers (CLI / cron run) do not consume follow-up
        # continuations, so they still need the current step or waiting-state response.
        defer_task_reply = (
            route.mode == "task"
            and task_state is not None
            and (
                continuation_scheduled
                or task_state.phase == "waiting_subagent"
            )
        )
        if defer_task_reply and not direct_return_required:
            return None

        if (
            direct_return_required
            and task_state is not None
            and task_state.phase == "waiting_subagent"
        ):
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="已委派后台任务，等待结果。",
                metadata=meta,
            )

        if silent_internal:
            if task_state is None:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=final_content,
                    metadata=meta,
                )
            if task_state.phase in {"blocked", "completed"}:
                content = final_content
                if task_state.phase == "blocked" and not content:
                    content = task_state.waiting_reason or "任务已暂停，等待进一步处理。"
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            if continuation_scheduled or task_state.phase == "waiting_subagent":
                return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Delegate to MemoryStore.consolidate() (pure trimming)."""
        await MemoryStore(self.workspace).consolidate(
            session,
            archive_all=archive_all,
            memory_window=self.memory_window,
        )

    def _should_run_memory_agent(
        self,
        msg_content: str,
        session: Session,
        memory_priority: str,
    ) -> bool:
        """判断是否需要运行 memory agent（智能触发）"""
        # 1. 优先级高 → 必须运行
        if memory_priority == "high":
            return True

        # 2. 简单对话跳过（可配置阈值）
        content_len = len(msg_content.strip())
        if content_len < 30:  # 少于 30 字符
            # 检查是否包含高优先级关键词
            msg_lower = msg_content.lower()
            if not any(kw in msg_lower for kw in _MEMORY_HIGH_KEYWORDS):
                return False

        # 3. 强制间隔：每 N 轮运行一次，避免长期遗漏
        turns_since_last = len(session.messages) - (session.last_memory_processed or 0)
        if turns_since_last >= self._memory_agent_interval:
            return True

        return False

    async def _run_memory_agent(
        self,
        session: Session,
        memory_priority: str = "normal",
        pre_search_results: list[dict] | None = None,
    ) -> None:
        """Run memory subagent in background to extract memories."""
        import time as _time

        from nano_alice.agent.memory_agent import MemoryAgent

        total = len(session.messages)
        cursor = session.last_memory_processed

        # Clamp stale cursor (e.g. after external edits)
        if cursor > total:
            cursor = max(0, total - 10)

        new_messages = session.messages[cursor:]
        if not new_messages:
            return

        # Up to 6 messages before cursor as read-only context (3 turns)
        context_start = max(0, cursor - 6)
        context_messages = session.messages[context_start:cursor] if cursor > 0 else None

        # Check if SCRATCH.md cleanup is due (48h)
        cleanup_scratch = False
        if self._memory_index:
            try:
                last_cleanup = self._memory_index.get_scratch_last_cleanup()
                if _time.time() - last_cleanup > 48 * 3600:
                    cleanup_scratch = True
            except Exception:
                pass

        agent = MemoryAgent(
            provider=self._memory_provider,
            workspace=self.workspace,
            model=self._memory_agent_model,
            embeddings_config=self.embeddings_config,
        )
        try:
            await agent.run(
                new_messages,
                context_messages=context_messages,
                cleanup_scratch=cleanup_scratch,
                memory_priority=memory_priority,
                pre_search_results=pre_search_results,  # RAG 结果复用
            )
            # Success: advance cursor and persist
            session.last_memory_processed = total
            self.sessions.save(session)
            # Update scratch cleanup timestamp on success
            if cleanup_scratch and self._memory_index:
                try:
                    self._memory_index.set_scratch_last_cleanup(_time.time())
                except Exception:
                    pass
        except Exception as e:
            logger.error("Memory agent failed: {}", e)
            # Cursor not advanced — next run will retry the same messages

    def _should_record_daily_cache(
        self,
        *,
        msg: InboundMessage,
        session_key: str | None,
        internal_task_event: bool,
        tool_events: list[dict[str, Any]],
    ) -> bool:
        if internal_task_event or msg.channel == "system":
            return False
        if session_key == "heartbeat" or msg.content.startswith("Read HEARTBEAT.md"):
            return False
        if msg.sender_id == "cron":
            return False
        return any(str(event.get("name") or "") in {"web_search", "web_fetch", "exec", "write_file", "edit_file", "spawn"} for event in tool_events)

    async def _record_daily_cache(
        self,
        *,
        session_key: str,
        trigger: str,
        tool_events: list[dict[str, Any]],
        final_content: str | None,
    ) -> None:
        try:
            records = self.daily_cache_recorder.build_records(
                session_key=session_key,
                trigger=trigger,
                tool_events=tool_events,
                final_content=final_content,
            )
            if records:
                self.daily_cache_store.append_records(records)
        except Exception as e:
            logger.warning("Daily cache recording failed: {}", e)

    async def run_memory_maintenance(self) -> dict[str, Any]:
        """Reconcile existing managed memory files globally."""
        from nano_alice.agent.memory_agent import MemoryAgent

        managed_files = [
            self.workspace / "memory" / "MEMORY.md",
            self.workspace / "memory" / "HISTORY.md",
            self.workspace / "memory" / "SCRATCH.md",
            self.workspace / "memory" / "projects.md",
            self.workspace / "memory" / "lessons.md",
            self.workspace / "memory" / "schedule.md",
        ]
        before: dict[str, str] = {}
        files_scanned: list[str] = []
        for path in managed_files:
            if path.exists():
                rel = str(path.relative_to(self.workspace)).replace("\\", "/")
                files_scanned.append(rel)
                before[rel] = path.read_text(encoding="utf-8")

        agent = MemoryAgent(
            provider=self._memory_provider,
            workspace=self.workspace,
            model=self._memory_agent_model,
            embeddings_config=self.embeddings_config,
        )
        try:
            summary = await agent.run_maintenance()
        except Exception as e:
            logger.error("Memory maintenance failed: {}", e)
            return {
                "status": "failed",
                "files_scanned": files_scanned,
                "files_modified": [],
                "summary": "",
                "error": str(e),
            }

        files_modified: list[str] = []
        for rel in files_scanned:
            path = self.workspace / rel
            after = path.read_text(encoding="utf-8") if path.exists() else ""
            if before.get(rel, "") != after:
                files_modified.append(rel)

        status = "done" if files_modified else "noop"
        return {
            "status": status,
            "files_scanned": files_scanned,
            "files_modified": files_modified,
            "summary": summary,
            "error": "",
        }

    @staticmethod
    def _format_memory_maintenance_result(result: dict[str, Any]) -> str:
        if result.get("status") == "failed":
            return f"记忆整理失败：{result.get('error') or 'unknown error'}"

        scanned = len(result.get("files_scanned") or [])
        modified = result.get("files_modified") or []
        summary = (result.get("summary") or "").strip()
        if not modified:
            base = f"记忆整理完成：扫描 {scanned} 个文件，未发现需要整理的内容。"
            return f"{base}\n{summary}" if summary else base

        base = f"记忆整理完成：扫描 {scanned} 个文件，更新 {len(modified)} 个文件。"
        detail = "更新文件：" + "、".join(modified)
        return f"{base}\n{detail}\n{summary}" if summary else f"{base}\n{detail}"

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(
            # Direct callers only inspect the return value, so keep the first visible
            # task response even when autorun schedules follow-up work in the bus.
            msg,
            session_key=session_key,
            on_progress=on_progress,
            direct_return_required=True,
        )
        return response.content if response else ""
