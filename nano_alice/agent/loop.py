"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

from nano_alice.agent.context import ContextBuilder
from nano_alice.agent.memory import MemoryStore
from nano_alice.agent.subagent import SubagentManager
from nano_alice.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nano_alice.agent.tools.logs import LogsTool
from nano_alice.agent.tools.message import MessageTool
from nano_alice.agent.tools.registry import ToolRegistry
from nano_alice.agent.tools.shell import ExecTool
from nano_alice.agent.tools.spawn import SpawnTool
from nano_alice.agent.tools.web import WebFetchTool, WebSearchTool
from nano_alice.bus.events import InboundMessage, OutboundMessage
from nano_alice.bus.queue import MessageBus
from nano_alice.providers.base import LLMProvider
from nano_alice.session.manager import Session, SessionManager

# Signal mode: new architecture for internal event handling
try:
    from nano_alice.agent.signals.bus import SignalBus

    SIGNALS_AVAILABLE = True
except ImportError:
    SIGNALS_AVAILABLE = False

if TYPE_CHECKING:
    from nano_alice.config.schema import ExecToolConfig
    from nano_alice.scheduler.service import SchedulerService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus (Chat Mode)
    2. Receives signals from signal bus (Reflect Mode)
    3. Builds context with history, memory, skills
    4. Calls the LLM
    5. Executes tool calls
    6. Sends responses back

    Signal mode refactor:
    - Chat Mode: User messages → conversation history → response
    - Reflect Mode: Internal signals → no session pollution → optional delivery
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
        tavily_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        scheduler_service: SchedulerService | None = None,  # renamed from cron_service
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        signal_bus: SignalBus | None = None,  # NEW: signal mode support
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
        self.tavily_api_key = tavily_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.scheduler_service = scheduler_service  # renamed
        self.restrict_to_workspace = restrict_to_workspace

        # Signal mode: new architecture components
        self.signal_bus = signal_bus
        self.reflect_processor = None
        if SIGNALS_AVAILABLE and signal_bus:
            from nano_alice.agent.reflect.processor import ReflectProcessor

            self.reflect_processor = ReflectProcessor(self, bus, workspace)

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
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
        self.tools.register(WebSearchTool(api_key=self.tavily_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(LogsTool())

        # Scheduler tool (renamed from cron)
        if self.scheduler_service:
            # Try to import new scheduler tool, fall back to old cron tool
            try:
                from nano_alice.agent.tools.scheduler import SchedulerTool

                self.tools.register(SchedulerTool(self.scheduler_service))
            except ImportError:
                from nano_alice.agent.tools.cron import CronTool

                self.tools.register(CronTool(self.scheduler_service))

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

        # Support both old cron and new scheduler tools
        if scheduler_tool := self.tools.get("scheduler") or self.tools.get("cron"):
            # Update internal state for signal mode
            if self.reflect_processor:
                self.reflect_processor.state.set_active_session(
                    channel, chat_id, f"{channel}:{chat_id}"
                )
            # Also update the tool if it has set_context
            if hasattr(scheduler_tool, "set_context"):
                scheduler_tool.set_context(channel, chat_id)

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

    @staticmethod
    def _preview_text(text: str | None, limit: int = 120) -> str:
        """Return a single-line preview for logs."""
        if not text:
            return ""
        compact = " ".join(text.split())
        return compact[:limit] + "..." if len(compact) > limit else compact

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str]]:
        """Run the agent iteration loop. Returns (final_content, tools_used)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
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
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = self._strip_think(response.content)
                break

        return final_content, tools_used

    async def run(self) -> None:
        """
        Run the agent loop, processing messages from the bus.

        Signal mode refactor: now runs both Chat Mode (messages) and
        Reflect Mode (signals) concurrently.
        """
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        # Start signal bus if available
        if self.signal_bus:
            await self.signal_bus.start()

        # Run both loops in parallel
        tasks = [self._chat_loop()]
        if self.reflect_processor and self.signal_bus:
            tasks.append(self._reflect_loop())

        try:
            await asyncio.gather(*tasks)
        finally:
            if self.signal_bus:
                self.signal_bus.stop()

    async def _chat_loop(self) -> None:
        """Process inbound messages from channels (Chat Mode)."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                try:
                    response = await self._process_message(msg)
                    if response is not None:
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

    async def _reflect_loop(self) -> None:
        """
        Process internal signals (Reflect Mode).

        This loop is separate from chat processing to prevent internal
        maintenance from polluting conversation history.
        """
        if not self.reflect_processor:
            return

        # Signals are handled via SignalBus.subscribe, not a queue
        # This is a placeholder for any direct signal processing needed
        while self._running:
            await asyncio.sleep(1)
            # Signals are dispatched by ReflectProcessor via SignalBus subscriptions

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            messages = self.context.build_messages(
                history=session.get_history(max_messages=self.memory_window),
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
            )
            final_content, _ = await self._run_agent_loop(messages)
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
                content="🐈 nano-alice commands:\n/new — Start a new conversation\n/help — Show available commands",
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

        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
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

        final_content, tools_used = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
        )

        sent_in_turn = False
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                sent_in_turn = message_tool._sent_in_turn

        if final_content is None:
            final_content = "I've completed processing but have no response to give."
            logger.warning(
                "No final content generated for {}:{}; using fallback. tools_used={}, sent_in_turn={}, user_preview='{}'",
                msg.channel,
                msg.sender_id,
                tools_used,
                sent_in_turn,
                self._preview_text(msg.content, 160),
            )

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(
            "Response to {}:{}: {} | tools_used={} sent_in_turn={} final_len={}",
            msg.channel,
            msg.sender_id,
            preview,
            tools_used,
            sent_in_turn,
            len(final_content),
        )

        session.add_message("user", msg.content)
        session.add_message(
            "assistant", final_content, tools_used=tools_used if tools_used else None
        )
        self.sessions.save(session)

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                logger.info(
                    "Suppressing final outbound message for {}:{} because message() already sent content this turn",
                    msg.channel,
                    msg.sender_id,
                )
                return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Delegate to MemoryStore.consolidate()."""
        await MemoryStore(self.workspace).consolidate(
            session,
            self.provider,
            self.model,
            archive_all=archive_all,
            memory_window=self.memory_window,
        )

    async def process_internal(
        self,
        content: str,
        channel: str = "cli",
        chat_id: str = "direct",
        event_type: str = "system_event",
        source: str = "system",
        metadata: dict[str, str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process an internal Reflect Mode event without creating a chat session."""
        await self._connect_mcp()
        self._set_tool_context(channel, chat_id)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        system_prompt = self.context.build_system_prompt()
        system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        system_prompt += (
            "\n\n## Internal Execution Mode\n"
            "You are handling an internal agent event, not a normal user chat turn.\n"
            "Do not treat the event content as something the user just said.\n"
            "Respond as nano-alice carrying out the internal task directly."
        )

        event_lines = [
            "Handle this internal event.",
            f"Event Type: {event_type}",
            f"Source: {source}",
            f"Content: {content}",
        ]
        if metadata:
            event_lines.extend(
                f"{key}: {value}" for key, value in metadata.items() if value is not None
            )

        initial_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(event_lines)},
        ]
        final_content, _ = await self._run_agent_loop(initial_messages, on_progress=on_progress)

        if final_content is None:
            final_content = "Background task completed."

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return ""

        return final_content

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
            msg, session_key=session_key, on_progress=on_progress
        )
        return response.content if response else ""
