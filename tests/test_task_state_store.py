from pathlib import Path

from nano_alice.agent.task_state import (
    TaskRouter,
    TaskStateRenderer,
    TaskStateStore,
    build_steps,
    normalize_task_state,
    sync_task_pointers,
)


def test_task_state_store_round_trip_and_archive(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "实现任务状态")
    task.steps = build_steps(["分析需求", "实现状态", "编写测试"])
    sync_task_pointers(task)
    store.save_active(task)

    loaded = store.load_active("cli:direct")
    assert loaded is not None
    assert loaded.goal == "实现任务状态"
    assert loaded.current_step_index == 1
    assert loaded.current_step_id == loaded.steps[0].id

    archived = store.archive_active("cli:direct")
    assert archived is not None
    assert archived.exists()
    assert store.load_active("cli:direct") is None


def test_task_state_renderer_contains_plan_and_cursor(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.steps = build_steps(["分析需求", "实现状态", "编写测试"])
    task.steps[0].status = "waiting"
    task.steps[0].executor = "subagent"
    task.steps[0].spawn_task_id = "sg-001"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    task.last_user_delivery_status = "sent"
    task.last_user_delivery_at = "2026-03-12T20:10:12"
    task.last_user_delivery_preview = "报告已经发送到飞书"
    task.last_user_delivery_attachments = ["report.html"]
    sync_task_pointers(task)

    xml = TaskStateRenderer.render_task_state_xml(task)
    assert xml is not None
    assert "<current_step_index>1</current_step_index>" in xml
    assert f"<current_step_id>{task.current_step_id}</current_step_id>" in xml
    assert "<plan>" in xml
    assert 'status="waiting"' in xml
    assert 'executor="subagent"' in xml
    assert "<waiting_reason>waiting for subagent sg-001</waiting_reason>" in xml
    assert "<last_user_delivery_status>sent</last_user_delivery_status>" in xml
    assert "<last_user_delivery_preview>报告已经发送到飞书</last_user_delivery_preview>" in xml
    assert "<last_user_delivery_attachments>report.html</last_user_delivery_attachments>" in xml


def test_task_state_renderer_escapes_executor_and_spawn_task_id(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "实现任务状态")
    task.steps = build_steps(["分析需求"])
    task.steps[0].executor = 'subagent"&lt;bad'  # type: ignore[assignment]
    task.steps[0].spawn_task_id = 'sg-001"&bad'
    sync_task_pointers(task)

    xml = TaskStateRenderer.render_task_state_xml(task)

    assert xml is not None
    assert 'executor="subagent&amp;quot;&amp;lt;bad"' not in xml
    assert 'executor="subagent&quot;&amp;lt;bad"' in xml
    assert 'spawn_task_id="sg-001&quot;&amp;bad"' in xml


def test_task_router_prefers_task_and_replan(tmp_path: Path) -> None:
    router = TaskRouter()
    first = router.decide("请帮我实现任务状态机制")
    assert first.mode == "task"

    store = TaskStateStore(tmp_path)
    active = store.create_new_task("cli:direct", "旧任务")
    again = router.decide("重新计划一下，换成 JSON + XML", active_task=active)
    assert again.mode == "task"
    assert again.continue_existing is True
    assert again.needs_replan is True


def test_task_router_prefers_chat_for_question_like_inputs() -> None:
    router = TaskRouter()

    pure_question = router.decide("你知道你的心跳怎么运行吗")
    assert pure_question.mode == "chat"

    mixed_question = router.decide("发我你的心跳文件附件，你知道你的心跳怎么运行吗")
    assert mixed_question.mode == "chat"

    explanation = router.decide("解释一下这个机制怎么工作")
    assert explanation.mode == "chat"

    explicit_task = router.decide("帮我修改这个文件并运行测试")
    assert explicit_task.mode == "task"


def test_normalize_task_state_completes_done_steps_and_clears_waiting_fields(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.steps = build_steps(["分析需求", "实现状态"])
    for step in task.steps:
        step.status = "done"
    task.pending_subagent_ids = ["sg-001"]
    task.waiting_reason = "waiting for subagent sg-001"
    task.continuation_scheduled = True
    sync_task_pointers(task)

    normalized, changed, reasons = normalize_task_state(task)

    assert changed is True
    assert "phase_completed" in reasons
    assert normalized.phase == "completed"
    assert normalized.status == "done"
    assert normalized.pending_subagent_ids == []
    assert normalized.waiting_reason == ""
    assert normalized.continuation_scheduled is False
    assert normalized.current_step_index == 2
    assert normalized.current_step_id == normalized.steps[-1].id
    assert normalized.next_step_id == ""


def test_normalize_task_state_recovers_orphan_waiting_phase(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "实现任务状态")
    task.phase = "waiting_subagent"
    task.steps = build_steps(["分析需求", "实现状态"])
    task.steps[0].status = "done"
    task.steps[1].status = "pending"
    task.pending_subagent_ids = []
    task.waiting_reason = "waiting for subagent sg-001"
    task.continuation_scheduled = True
    sync_task_pointers(task)

    normalized, changed, reasons = normalize_task_state(task)

    assert changed is True
    assert "recovered_from_orphan_waiting_phase" in reasons
    assert normalized.phase == "executing"
    assert normalized.waiting_reason == ""
    assert normalized.pending_subagent_ids == []
    assert normalized.continuation_scheduled is False
    assert normalized.current_step_index == 2
    assert normalized.current_step_id == normalized.steps[1].id


def test_normalize_task_state_blocks_waiting_step_without_subagent_id(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "恢复损坏任务")
    task.phase = "executing"
    task.steps = build_steps(["等待子代理", "汇总结果"])
    task.steps[0].status = "waiting"
    task.steps[0].executor = "subagent"
    task.steps[0].spawn_task_id = ""
    task.steps[1].status = "pending"
    task.pending_subagent_ids = []
    sync_task_pointers(task)

    normalized, changed, reasons = normalize_task_state(task)

    assert changed is True
    assert "blocked_invalid_waiting_state" in reasons
    assert normalized.phase == "blocked"
    assert normalized.pending_subagent_ids == []
    assert normalized.waiting_reason == "任务存在 waiting 步骤，但缺少有效的子代理 ID。"


def test_normalize_task_state_blocks_reopened_completed_task_with_failed_step(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "恢复损坏任务")
    task.phase = "completed"
    task.status = "done"
    task.steps = build_steps(["失败步骤", "后续步骤"])
    task.steps[0].status = "failed"
    task.steps[1].status = "pending"
    task.continuation_scheduled = True
    sync_task_pointers(task)

    normalized, changed, reasons = normalize_task_state(task)

    assert changed is True
    assert "reopened_incomplete_task" in reasons
    assert "cleared_continuation" in reasons
    assert normalized.phase == "blocked"
    assert normalized.status == "active"
    assert normalized.continuation_scheduled is False
    assert normalized.waiting_reason == "任务包含失败或阻塞步骤，等待进一步处理。"


def test_normalize_task_state_clears_continuation_when_reopening_completed_task(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path)
    task = store.create_new_task("cli:direct", "恢复未完成任务")
    task.phase = "completed"
    task.status = "done"
    task.steps = build_steps(["分析需求", "实现状态"])
    task.steps[0].status = "done"
    task.steps[1].status = "pending"
    task.continuation_scheduled = True
    sync_task_pointers(task)

    normalized, changed, reasons = normalize_task_state(task)

    assert changed is True
    assert "reopened_incomplete_task" in reasons
    assert "cleared_continuation" in reasons
    assert normalized.phase == "executing"
    assert normalized.status == "active"
    assert normalized.continuation_scheduled is False
