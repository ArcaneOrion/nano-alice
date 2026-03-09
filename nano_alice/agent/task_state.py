"""Task state management for plan-first execution."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from nano_alice.utils.helpers import ensure_dir, safe_filename

TaskMode = Literal["chat", "task"]
TaskPhase = Literal["planning", "executing", "waiting_subagent", "blocked", "replanning", "completed"]
TaskStatus = Literal["active", "done", "failed", "cancelled"]
TaskStepStatus = Literal["pending", "in_progress", "waiting", "done", "failed", "blocked"]
TaskExecutor = Literal["main", "subagent"]

_TASK_HINTS = (
    "帮我", "请你", "实现", "修改", "修复", "创建", "新增", "设计", "重构", "运行",
    "检查", "分析代码", "继续", "下一步", "进度", "整理", "排查", "添加", "更新",
    "implement", "fix", "create", "add", "update", "refactor", "run", "check",
    "continue", "next step", "progress", "design",
)
_CHAT_HINTS = ("是什么", "为什么", "怎么理解", "解释", "介绍", "what is", "why", "explain")
_REPLAN_HINTS = ("重新计划", "重排", "改计划", "换成", "其实", "不要", "replan", "instead", "change")


def _escape_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


@dataclass
class TaskStep:
    index: int
    id: str
    title: str
    status: TaskStepStatus = "pending"
    executor: TaskExecutor = "main"
    result: str = ""
    evidence: list[str] = field(default_factory=list)
    notes: str = ""
    spawn_task_id: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskState:
    version: int
    session_key: str
    mode: TaskMode
    phase: TaskPhase
    status: TaskStatus
    task_id: str
    goal: str
    summary: str
    plan_version: int
    current_step_index: int
    current_step_id: str
    next_step_id: str
    steps: list[TaskStep] = field(default_factory=list)
    strategy: str = "先规划后执行；每次只推进一个步骤"
    rejected_options: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=lambda: ["每个 session 同时只允许一个 active task"])
    open_questions: list[str] = field(default_factory=list)
    last_action: str = ""
    last_event: str = ""
    waiting_reason: str = ""
    pending_subagent_ids: list[str] = field(default_factory=list)
    auto_run_count: int = 0
    failure_count: int = 0
    continuation_scheduled: bool = False
    max_auto_runs: int = 8
    max_failures: int = 2
    max_replans: int = 2
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        steps = [TaskStep(**step) for step in data.get("steps", [])]
        return cls(
            version=data.get("version", 1),
            session_key=data["session_key"],
            mode=data.get("mode", "task"),
            phase=data.get("phase", "planning"),
            status=data.get("status", "active"),
            task_id=data["task_id"],
            goal=data.get("goal", ""),
            summary=data.get("summary", ""),
            plan_version=data.get("plan_version", 1),
            current_step_index=data.get("current_step_index", 1),
            current_step_id=data.get("current_step_id", ""),
            next_step_id=data.get("next_step_id", ""),
            steps=steps,
            strategy=data.get("strategy", "先规划后执行；每次只推进一个步骤"),
            rejected_options=data.get("rejected_options", []),
            assumptions=data.get("assumptions", ["每个 session 同时只允许一个 active task"]),
            open_questions=data.get("open_questions", []),
            last_action=data.get("last_action", ""),
            last_event=data.get("last_event", ""),
            waiting_reason=data.get("waiting_reason", ""),
            pending_subagent_ids=data.get("pending_subagent_ids", []),
            auto_run_count=data.get("auto_run_count", 0),
            failure_count=data.get("failure_count", 0),
            continuation_scheduled=data.get("continuation_scheduled", False),
            max_auto_runs=data.get("max_auto_runs", 8),
            max_failures=data.get("max_failures", 2),
            max_replans=data.get("max_replans", 2),
            updated_at=data.get("updated_at", datetime.now().isoformat(timespec="seconds")),
        )


@dataclass
class TaskRouteDecision:
    mode: TaskMode
    reason: str
    continue_existing: bool = False
    needs_replan: bool = False


class TaskRouter:
    """Route incoming messages into chat/task modes."""

    def decide(self, content: str, active_task: TaskState | None = None) -> TaskRouteDecision:
        text = content.strip().lower()
        if active_task:
            if any(hint in text for hint in _REPLAN_HINTS):
                return TaskRouteDecision("task", "user_requested_replan", True, True)
            return TaskRouteDecision("task", "active_task_continues", True)

        if any(hint in text for hint in _TASK_HINTS):
            return TaskRouteDecision("task", "task_like_request")
        if any(hint in text for hint in _CHAT_HINTS):
            return TaskRouteDecision("chat", "question_like_request")
        if len(text) > 40 and any(ch in text for ch in ("。", ".", "，", ",")):
            return TaskRouteDecision("task", "long_structured_request")
        return TaskRouteDecision("chat", "default_chat")


class TaskStateStore:
    """Persist the current active task and archive finished tasks."""

    def __init__(self, workspace: Path):
        self.root = ensure_dir(workspace / "task_state")
        self.active_dir = ensure_dir(self.root / "active")
        self.archive_dir = ensure_dir(self.root / "archive")

    def _active_path(self, session_key: str) -> Path:
        return self.active_dir / f"{safe_filename(session_key.replace(':', '_'))}.json"

    def load_active(self, session_key: str) -> TaskState | None:
        path = self._active_path(session_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None
        return TaskState.from_dict(data)

    def save_active(self, task_state: TaskState) -> None:
        task_state.updated_at = datetime.now().isoformat(timespec="seconds")
        self._active_path(task_state.session_key).write_text(
            json.dumps(task_state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear_active(self, session_key: str) -> None:
        path = self._active_path(session_key)
        if path.exists():
            path.unlink()

    def archive_active(self, session_key: str) -> Path | None:
        path = self._active_path(session_key)
        if not path.exists():
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = self.archive_dir / f"{safe_filename(session_key.replace(':', '_'))}__{ts}.json"
        path.rename(archived)
        return archived

    def create_new_task(self, session_key: str, goal: str, summary: str = "") -> TaskState:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return TaskState(
            version=1,
            session_key=session_key,
            mode="task",
            phase="planning",
            status="active",
            task_id=f"task_{ts}",
            goal=goal.strip(),
            summary=summary.strip() or goal.strip(),
            plan_version=1,
            current_step_index=1,
            current_step_id="",
            next_step_id="",
        )


class TaskStateRenderer:
    """Render hard-coded rules and current task state into XML."""

    @staticmethod
    def render_task_rules_xml() -> str:
        return """<task_execution_rules>
  <rule>当 mode=task 时，必须先输出或确认计划，再执行。</rule>
  <rule>当 phase=planning 时，不得声称任务已执行完成。</rule>
  <rule>当 phase=executing 时，只能执行 current_step_index 对应步骤。</rule>
  <rule>当 phase=waiting_subagent 时，必须等待对应子代理结果，不得直接跳到下一步。</rule>
  <rule>未完成当前步骤前，不得跳到后续步骤。</rule>
  <rule>只有当步骤存在 evidence 或明确结果时，才能将其标记为 done。</rule>
  <rule>若发现计划不成立，可进入 replanning，但必须先更新计划再继续执行。</rule>
  <rule>若收到 continuation 事件，应基于 task_state 继续当前步骤，不要把它当作新任务。</rule>
  <rule>若任务需要高风险操作或缺少关键信息，必须进入 blocked 并等待用户确认。</rule>
</task_execution_rules>"""

    @staticmethod
    def render_task_state_xml(task_state: TaskState | None) -> str | None:
        if task_state is None or task_state.mode != "task":
            return None
        plan_lines = []
        for step in task_state.steps:
            title = escape(step.title)
            plan_lines.append(
                f'    <step index="{step.index}" id="{_escape_attr(step.id)}" status="{step.status}" executor="{_escape_attr(step.executor)}" spawn_task_id="{_escape_attr(step.spawn_task_id)}">{title}</step>'
            )
        rejected = TaskStateRenderer._render_items("rejected_options", task_state.rejected_options)
        assumptions = TaskStateRenderer._render_items("assumptions", task_state.assumptions)
        open_questions = TaskStateRenderer._render_items("open_questions", task_state.open_questions)
        summary = escape(task_state.summary)
        return f"""<task_state>
  <mode>{task_state.mode}</mode>
  <phase>{task_state.phase}</phase>
  <status>{task_state.status}</status>
  <task_id>{escape(task_state.task_id)}</task_id>
  <goal>{escape(task_state.goal)}</goal>
  <summary>{summary}</summary>
  <plan_meta>
    <plan_version>{task_state.plan_version}</plan_version>
    <current_step_index>{task_state.current_step_index}</current_step_index>
    <current_step_id>{escape(task_state.current_step_id)}</current_step_id>
    <next_step_id>{escape(task_state.next_step_id)}</next_step_id>
  </plan_meta>
  <execution>
    <last_action>{escape(task_state.last_action)}</last_action>
    <last_event>{escape(task_state.last_event)}</last_event>
    <waiting_reason>{escape(task_state.waiting_reason)}</waiting_reason>
    <pending_subagent_ids>{escape(",".join(task_state.pending_subagent_ids))}</pending_subagent_ids>
    <auto_run_count>{task_state.auto_run_count}</auto_run_count>
    <failure_count>{task_state.failure_count}</failure_count>
    <continuation_scheduled>{str(task_state.continuation_scheduled).lower()}</continuation_scheduled>
    <max_auto_runs>{task_state.max_auto_runs}</max_auto_runs>
    <max_failures>{task_state.max_failures}</max_failures>
  </execution>
  <plan>
{chr(10).join(plan_lines) if plan_lines else '    <step index="1" id="pending" status="pending">待生成计划</step>'}
  </plan>
  <strategy>{escape(task_state.strategy)}</strategy>
{rejected}
{assumptions}
{open_questions}
</task_state>"""

    @staticmethod
    def _render_items(tag: str, items: list[str]) -> str:
        if not items:
            return f"  <{tag} />"
        lines = [f"    <item>{escape(item)}</item>" for item in items]
        return f"  <{tag}>\n" + "\n".join(lines) + f"\n  </{tag}>"


def extract_plan_from_text(content: str) -> list[str]:
    """Extract an ordered plan from assistant text."""
    if not content:
        return []
    code_block = re.search(r"```(?:json)?\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```", content)
    json_blob = code_block.group(1) if code_block else content
    try:
        parsed = json.loads(json_blob)
        if isinstance(parsed, dict):
            steps = parsed.get("steps", [])
        else:
            steps = parsed
        titles = []
        for item in steps:
            if isinstance(item, str):
                titles.append(item.strip())
            elif isinstance(item, dict):
                titles.append(str(item.get("title") or item.get("step") or "").strip())
        return [title for title in titles if title]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    titles = []
    for line in content.splitlines():
        stripped = line.strip()
        stripped = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", stripped)
        if stripped and len(stripped) <= 120 and stripped != content.strip():
            titles.append(stripped)
    return titles[:7]


def build_steps(plan_lines: list[str]) -> list[TaskStep]:
    steps: list[TaskStep] = []
    for idx, title in enumerate(plan_lines, start=1):
        step_id = safe_filename(title.lower().replace(" ", "_")).strip("_") or f"step_{idx}"
        steps.append(TaskStep(index=idx, id=step_id, title=title, status="pending"))
    if steps:
        steps[0].status = "in_progress"
    return steps


def sync_task_pointers(task_state: TaskState) -> TaskState:
    current = next((step for step in task_state.steps if step.status == "in_progress"), None)
    if current is None:
        current = next((step for step in task_state.steps if step.status == "waiting"), None)
    if current is None and task_state.phase == "blocked":
        current = next((step for step in task_state.steps if step.status == "blocked"), None)
    if current is None and task_state.phase == "blocked":
        current = next((step for step in task_state.steps if step.status == "failed"), None)
    if current is None:
        current = next((step for step in task_state.steps if step.status == "pending"), None)
        if current is not None:
            current.status = "in_progress"

    if current is None:
        task_state.current_step_index = len(task_state.steps) if task_state.steps else 0
        task_state.current_step_id = ""
        task_state.next_step_id = ""
        return task_state

    task_state.current_step_index = current.index
    task_state.current_step_id = current.id
    next_step = next((step for step in task_state.steps if step.index == current.index + 1), None)
    task_state.next_step_id = next_step.id if next_step else ""
    return task_state
