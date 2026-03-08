"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# The prompt sent to agent during heartbeat
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
Decide whether the result should be pushed to the user's chat.

Return exactly one JSON object and nothing else.

If there is nothing worth pushing, return:
{"should_push": false, "reason": "brief reason", "content": ""}

If there is something worth pushing, return:
{"should_push": true, "reason": "brief reason", "content": "message to send to the user"}

Rules:
- `should_push` must be a boolean.
- `content` must contain the full message to send when `should_push` is true.
- `content` must be an empty string when `should_push` is false.
- Do not output markdown fences, explanations, or any extra text outside the JSON object."""

# Token that indicates "nothing to do"
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


def heartbeat_response_preview(response: str | None, limit: int = 200) -> str:
    """Return a single-line preview of the raw heartbeat response for logging."""
    if response is None:
        return ""

    preview = " ".join(response.strip().split())
    if len(preview) <= limit:
        return preview
    return preview[:limit] + "..."


@dataclass(frozen=True)
class HeartbeatDecision:
    """Structured decision returned by the heartbeat model."""

    should_push: bool
    content: str
    reason: str = ""


def parse_heartbeat_decision(response: str | None) -> HeartbeatDecision | None:
    """Parse the heartbeat model response into a structured decision."""
    if response is None:
        return None

    text = response.strip()
    if not text:
        return None

    candidate = text
    fenced_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced_match:
        candidate = fenced_match.group(1).strip()

    if not candidate.startswith("{"):
        return None

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or not isinstance(data.get("should_push"), bool):
        return None

    content = data.get("content", "")
    reason = data.get("reason", "")
    if content is None:
        content = ""
    if reason is None:
        reason = ""
    if not isinstance(content, str) or not isinstance(reason, str):
        return None

    content = content.strip()
    reason = reason.strip()
    should_push = data["should_push"]

    if should_push and not content:
        return None
    if not should_push:
        content = ""

    return HeartbeatDecision(should_push=should_push, content=content, reason=reason)


def normalize_heartbeat_response(response: str | None) -> tuple[HeartbeatDecision | None, str]:
    """Normalize heartbeat response for storage and downstream dispatch."""
    decision = parse_heartbeat_decision(response)
    if decision is None:
        return None, (response or "")
    if decision.should_push:
        return decision, decision.content
    return decision, HEARTBEAT_OK_TOKEN


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable content."""
    if not content:
        return True
    
    # Lines to skip: empty, headers, HTML comments, empty checkboxes
    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}
    
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False  # Found actionable content
    
    return True


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    The agent reads HEARTBEAT.md from the workspace and executes any
    tasks listed there. If nothing needs attention, it replies HEARTBEAT_OK.
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str, str, str], Coroutine[Any, Any, str]] | None = None,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
        notify_channel: str = "",
        notify_chat_id: str = "",
    ):
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat
        self.interval_s = interval_s
        self.enabled = enabled
        self.notify_channel = notify_channel
        self.notify_chat_id = notify_chat_id
        self._running = False
        self._task: asyncio.Task | None = None
    
    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"
    
    def _read_heartbeat_file(self) -> str | None:
        """Read HEARTBEAT.md content."""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None
    
    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)
    
    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
    
    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)
    
    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()
        
        # Skip if HEARTBEAT.md is empty or doesn't exist
        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return
        
        logger.info("Heartbeat: checking for tasks...")
        
        if self.on_heartbeat:
            try:
                response = await self.on_heartbeat(
                    HEARTBEAT_PROMPT, self.notify_channel, self.notify_chat_id
                )

                decision, _ = normalize_heartbeat_response(response)
                if decision is not None:
                    if decision.should_push:
                        logger.info("Heartbeat: completed task ({})", decision.reason or "push")
                    else:
                        logger.info("Heartbeat: OK ({})", decision.reason or "no action needed")
                elif response.strip():
                    logger.warning("Heartbeat returned non-structured response; falling back to raw content")
                elif self.notify_channel and self.notify_chat_id:
                    logger.info("Heartbeat: queued for {}:{}", self.notify_channel, self.notify_chat_id)
                else:
                    logger.info("Heartbeat: completed task")

            except Exception as e:
                logger.error("Heartbeat execution failed: {}", e)

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        if self.on_heartbeat:
            return await self.on_heartbeat(
                HEARTBEAT_PROMPT, self.notify_channel, self.notify_chat_id
            )
        return None
