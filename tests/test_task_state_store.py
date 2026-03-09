from pathlib import Path

from nano_alice.agent.task_state import TaskStateRenderer, TaskStateStore, TaskRouter, build_steps, sync_task_pointers


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
    sync_task_pointers(task)

    xml = TaskStateRenderer.render_task_state_xml(task)
    assert xml is not None
    assert "<current_step_index>1</current_step_index>" in xml
    assert f"<current_step_id>{task.current_step_id}</current_step_id>" in xml
    assert "<plan>" in xml
    assert 'status="waiting"' in xml
    assert 'executor="subagent"' in xml
    assert "<waiting_reason>waiting for subagent sg-001</waiting_reason>" in xml


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
