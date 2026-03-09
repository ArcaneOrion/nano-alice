"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nano_alice.agent.memory import MemoryStore
from nano_alice.agent.skills import SkillsLoader
from nano_alice.logging_utils import payload_bytes, summarize_tool_result


@dataclass
class BuiltContext:
    """Structured prompt state before rendering to provider messages."""

    system_prompt: str
    history_messages: list[dict[str, Any]]
    current_context_text: str
    current_user_input: str
    current_user_media: list[str] | None = None


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        task_rules_xml: str | None = None,
        task_state_xml: str | None = None,
    ) -> str:
        """
        Build the system prompt from bootstrap files, memory, and task state.

        Returns:
            Complete system prompt in XML format.
        """
        identity = self._get_identity()
        bootstrap = self._load_bootstrap_files()
        memory = self.memory.get_memory_context() or "(无)"
        task_rules = task_rules_xml or ""
        task_state = task_state_xml or ""

        result = f"""<system>
  <identity>
{identity}
  </identity>
  <bootstrap>
{bootstrap}
  </bootstrap>
  <memory>
{memory}
  </memory>
  <task>
{task_rules}
{task_state}
  </task>

</system>"""
        logger.debug("system prompt: {} chars", len(result))
        return result

    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        import time as _time

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = (
            f"{'macOS' if system == 'Darwin' else system} "
            f"{platform.machine()}, Python {platform.python_version()}"
        )

        return f"""# nano-alice 🐈

You are nano-alice, a helpful AI assistant.

## Current Time
{now} ({tz})

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Semantic search: use the memory_search tool for meaning-based recall
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. Before calling tools, briefly tell the user what you're about to do (one short sentence in the user's language).
If you need to use tools, call them directly — never send a preliminary message like "Let me check" without actually calling a tool.
When remembering something important, write to {workspace_path}/memory/MEMORY.md
To recall past events, grep {workspace_path}/memory/HISTORY.md"""

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        loaded = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
                loaded.append(filename)

        logger.debug("bootstrap files loaded: {}", loaded)
        return "\n\n".join(parts) if parts else ""

    def build_current_context_text(self, recalled_context: str | None = None) -> str:
        """Build the current-turn context block injected alongside user input."""
        recalled = recalled_context or "(无)"
        skills_summary = self.skills.build_skills_summary() or "(无)"
        logger.debug("skills summary: {} chars", len(skills_summary))
        return f"""<context>
  <memory>
    <recalled>{recalled}</recalled>
  </memory>
  <skills>
{skills_summary}
  </skills>
</context>"""

    def build_prompt_envelope(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        recalled_context: str | None = None,
        task_rules_xml: str | None = None,
        task_state_xml: str | None = None,
    ) -> BuiltContext:
        """Build structured prompt context before rendering messages."""
        del skill_names

        system_prompt = self.build_system_prompt(
            task_rules_xml=task_rules_xml,
            task_state_xml=task_state_xml,
        )
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"

        logger.debug("build_prompt_envelope: history={}, media={}", len(history), len(media) if media else 0)
        return BuiltContext(
            system_prompt=system_prompt,
            history_messages=self._prepare_history(history),
            current_context_text=self.build_current_context_text(recalled_context=recalled_context),
            current_user_input=current_message,
            current_user_media=media,
        )

    def render_messages(self, envelope: BuiltContext) -> list[dict[str, Any]]:
        """Render the structured prompt envelope to provider-compatible messages."""
        messages = [{"role": "system", "content": envelope.system_prompt}]
        messages.extend(envelope.history_messages)

        from datetime import datetime as _dt

        ts = _dt.now().strftime("%Y-%m-%d %H:%M")
        current_text = (
            f"{envelope.current_context_text}\n\n"
            f"<user_input>\n[{ts}] {envelope.current_user_input}\n</user_input>"
        )
        messages.append({
            "role": "user",
            "content": self._build_user_content(current_text, envelope.current_user_media),
        })
        return messages

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        recalled_context: str | None = None,
        task_rules_xml: str | None = None,
        task_state_xml: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper returning rendered provider messages."""
        envelope = self.build_prompt_envelope(
            history=history,
            current_message=current_message,
            skill_names=skill_names,
            media=media,
            channel=channel,
            chat_id=chat_id,
            recalled_context=recalled_context,
            task_rules_xml=task_rules_xml,
            task_state_xml=task_state_xml,
        )
        messages = self.render_messages(envelope)
        logger.debug("build_messages: history={}, media={}", len(history), len(media) if media else 0)
        return messages

    def compute_context_metrics(self, envelope: BuiltContext, messages: list[dict[str, Any]]) -> dict[str, int]:
        """Compute context metrics matching the structured prompt layout."""
        history_chars = sum(self._content_text_length(m.get("content")) for m in envelope.history_messages)
        total_chars = sum(self._content_text_length(m.get("content")) for m in messages)
        return {
            "system_chars": len(envelope.system_prompt),
            "history_chars": history_chars,
            "current_context_chars": len(envelope.current_context_text),
            "user_input_chars": len(envelope.current_user_input),
            "total_rendered_input_chars": total_chars,
            "history_message_count": len(envelope.history_messages),
        }

    def _prepare_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize history for provider consumption without changing session records."""
        prepared = []
        for msg in history:
            if msg["role"] == "user" and msg.get("media"):
                msg = {**msg, "content": self._build_user_content(msg["content"], msg["media"])}
                msg.pop("media", None)
            prepared.append(msg)
        return prepared

    def _content_text_length(self, content: Any) -> int:
        """Approximate textual payload size without counting binary/image blocks."""
        if content is None:
            return 0
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(self._content_text_length(item) for item in content)
        if isinstance(content, dict):
            if content.get("type") in {"text", "input_text", "output_text"}:
                return len(str(content.get("text", "")))
            return 0
        return len(str(content))

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str | list,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        summary = summarize_tool_result(tool_name, result)
        logger.debug(
            "add_tool_result: tool={} tool_call_id={} result_bytes={} result_kind={} message_bytes={} preview={}",
            tool_name,
            tool_call_id,
            summary["result_bytes"],
            summary["result_kind"],
            payload_bytes(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": result,
                }
            ),
            summary["preview"],
        )
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        })
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = content

        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages
