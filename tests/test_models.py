from pathlib import Path

from hermes_live_ui_controller.models import RunTask, SnapshotState


def test_load_manifest_from_file() -> None:
    task_path = Path("examples/task_edit_hero.json")
    task = RunTask.from_path(task_path)

    assert task.task_id == "edit-hero-section-2026-05-24"
    assert task.policy.allowed_domains == ["hermes-staging.local"]
    assert len(task.steps) == 5
    assert task.steps[1].action.type == "click"


def test_snapshot_state_helpers() -> None:
    state = SnapshotState(ref_ids=["a", "b"], text="Hero Text", title="Dash", url="https://x")
    assert state.normalized_text == "hero text"
    assert state.ref_ids == ["a", "b"]
