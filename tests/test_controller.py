import json
from pathlib import Path

from hermes_live_ui_controller.adapter import InMemoryBrowserAdapter
from hermes_live_ui_controller.controller import LiveWebUIRunner
from hermes_live_ui_controller.models import RunTask


def build_pages() -> dict:
    pages_file = Path("examples/pages_mock.json")
    return json.loads(pages_file.read_text(encoding="utf-8"))


def test_run_task_edit_hero_success_with_retries_and_confirm_disabled() -> None:
    task = RunTask.from_path("examples/task_edit_hero.json")
    pages = build_pages()
    adapter = InMemoryBrowserAdapter(pages=pages, start_url=pages["start_url"])
    runner = LiveWebUIRunner(adapter=adapter, auto_confirm=True, output_dir=".runs")

    result = runner.run(task)
    assert result.success is True
    assert result.failures == 0
    assert len(result.steps) == len(task.steps)
    assert all(step.success for step in result.steps)


def test_run_fails_when_policy_blocks_action() -> None:
    task = RunTask.from_path("examples/task_submit_form.json")
    pages = build_pages()
    task.steps[3].action.type = "click"  # keep same
    task.steps[1].action.ref = "delete_user"
    # mutate policy explicitly to demonstrate block behavior
    task.policy.blocked_actions.append("delete_user")

    adapter = InMemoryBrowserAdapter(pages=pages, start_url=pages["start_url"])
    runner = LiveWebUIRunner(adapter=adapter, auto_confirm=True)
    result = runner.run(task)

    assert result.success is False
    assert result.steps[0].success is True
    assert result.steps[1].success is False
    assert "blocked" in (result.steps[1].error or "").lower()
