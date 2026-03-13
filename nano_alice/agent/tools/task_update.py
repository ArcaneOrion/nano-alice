"""Tool for explicit task step updates."""

from __future__ import annotations

import json
from typing import Any

from nano_alice.agent.task_state import TaskStateStore, sync_task_pointers
from nano_alice.agent.tools.base import Tool


class TaskUpdateTool(Tool):
    """Update task step state explicitly."""

    def __init__(self, store: TaskStateStore):
        self._store = store
        self._default_session_key: str = ""
        self._default_task_id: str = ""

    def set_context(self, *, session_key: str = "", task_id: str = "") -> None:
        self._default_session_key = session_key
        self._default_task_id = task_id

    @property
    def name(self) -> str:
        return "task_update"

    @property
    def description(self) -> str:
        return (
            "Update the current task step state. Use this to mark a step done/blocked/waiting "
            "and decide whether to continue or complete the task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "step_index": {"type": "integer", "description": "Step index to update"},
                "status": {
                    "type": "string",
                    "enum": ["done", "blocked", "waiting"],
                    "description": "Step status update",
                },
                "result": {"type": "string", "description": "Short result summary"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence strings to attach",
                },
                "waiting_reason": {"type": "string", "description": "Reason for blocked/waiting"},
                "should_continue": {
                    "type": "boolean",
                    "description": "Advance to next step and schedule continuation",
                },
                "task_complete": {
                    "type": "boolean",
                    "description": "Mark task complete and stop continuation",
                },
            },
            "required": ["status"],
        }

    async def execute(
        self,
        status: str,
        step_index: int | None = None,
        result: str | None = None,
        evidence: list[str] | None = None,
        waiting_reason: str | None = None,
        should_continue: bool | None = None,
        task_complete: bool | None = None,
        **_: Any,
    ) -> str:
        session_key = self._default_session_key
        if not session_key:
            return "Error: task_update missing session context"

        task_state = self._store.load_active(session_key)
        if task_state is None:
            return "Error: no active task"
        if self._default_task_id and self._default_task_id != task_state.task_id:
            return "Error: task_id mismatch"

        idx = step_index if step_index is not None else task_state.current_step_index
        step = next((s for s in task_state.steps if s.index == idx), None)
        if step is None:
            return f"Error: step {idx} not found"

        step.result = (result or step.result)[:500]
        if evidence:
            step.evidence.extend(evidence)
        step.status = status
        task_state.last_action = f"task_update: step {idx} -> {status}"
        task_state.last_event = "task_update"
        task_state.continuation_scheduled = False

        should_continue = bool(should_continue)
        task_complete = bool(task_complete)
        reason = waiting_reason or (result or "")

        if status == "blocked":
            task_state.phase = "blocked"
            task_state.waiting_reason = reason[:500] or "等待用户确认继续。"
        elif status == "waiting":
            task_state.phase = "waiting_subagent"
            task_state.waiting_reason = reason[:500] or "等待后续结果。"
        elif status == "done":
            if task_complete:
                task_state.phase = "completed"
                task_state.status = "done"
                task_state.waiting_reason = ""
            elif should_continue:
                next_step = next((s for s in task_state.steps if s.index == idx + 1), None)
                if next_step is None:
                    task_state.phase = "completed"
                    task_state.status = "done"
                    task_state.waiting_reason = ""
                    task_complete = True
                else:
                    next_step.status = "in_progress"
                    task_state.phase = "executing"
                    task_state.waiting_reason = ""
            else:
                task_state.phase = "blocked"
                task_state.waiting_reason = "等待用户确认继续下一步。"

        sync_task_pointers(task_state)
        self._store.save_active(task_state)

        return json.dumps(
            {
                "ok": True,
                "task_id": task_state.task_id,
                "step_index": idx,
                "status": status,
                "phase": task_state.phase,
                "should_continue": should_continue,
                "task_complete": task_complete,
            },
            ensure_ascii=False,
        )
