from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_live_ui_controller.models import RunTask, SnapshotState
from hermes_live_ui_controller.web_server import build_app


class _FakeAdapter:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def snapshot(self, include_full: bool = True) -> SnapshotState:
        self.snapshot_calls += 1
        return SnapshotState(
            ref_ids=["btn-save", "form-name"],
            text="start",
            title="Fake UI",
            url="https://example.com",
            console_errors=0,
            meta={"snapshot_calls": self.snapshot_calls},
        )

    def close(self) -> None:
        pass

    def click(self, _ref: str) -> None:
        pass

    def type(self, _ref: str, _text: str) -> None:
        pass

    def scroll(self, _direction: str, _amount: int) -> None:
        pass

    def press(self, _key: str) -> None:
        pass

    def back(self) -> None:
        pass

    def navigate(self, _url: str) -> None:
        pass

    def console(self, clear: bool = False, expression: str | None = None):  # noqa: ARG002
        return ["ok"] if clear else []


def _build_task() -> dict:
    return RunTask(
        task_id="ui-task",
        goal="edit",
        url="https://example.com",
        steps=[],
    ).model_dump(mode="json")


def test_dashboard_html() -> None:
    client = TestClient(build_app("/tmp/does-not-exist-web-ui-data"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Hermes Live UI Controller" in response.text


def test_live_session_and_action_flow(monkeypatch) -> None:
    client = TestClient(build_app("/tmp/live-ui-data-session"))
    fake_adapter = _FakeAdapter()
    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: fake_adapter)

    response = client.post("/api/sessions", json={"url": "https://example.com"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("ws_")

    session_id = payload["id"]
    action = client.post(f"/api/sessions/{session_id}/action", json={"type": "click", "ref": "btn-save"})
    assert action.status_code == 200
    assert action.json()["ok"] is True


@pytest.mark.parametrize("complexity,expected_status", [("complex", "waiting_subagents"), ("simple", "todo")])
def test_kanban_status_from_complexity(complexity: str, expected_status: str) -> None:
    client = TestClient(build_app("/tmp/live-ui-data-kanban"))
    response = client.post(
        "/api/kanban",
        json={
            "title": "Neue Funktion",
            "description": "Komplexe Aufgabe prüfen",
            "complexity": complexity,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == expected_status


def test_kanban_handoff() -> None:
    client = TestClient(build_app("/tmp/live-ui-data-kanban-handoff"))
    response = client.post(
        "/api/kanban",
        json={
            "title": "Neue Funktion",
            "description": "komplizierte Aufgabe",
            "complexity": "simple",
        },
    )
    task = response.json()

    handoff = client.post(f"/api/kanban/{task['id']}/handoff")
    assert handoff.status_code == 200

    updated = handoff.json()
    assert updated["status"] == "waiting_subagents"
    assert "handoff_payload" in updated


def test_run_api_async_success(monkeypatch) -> None:
    client = TestClient(build_app("/tmp/live-ui-data-run"))

    class _Runner:
        def __init__(self, *_, **__):
            pass

        def run(self, task: RunTask):
            del task
            return SimpleNamespace(
                success=True,
                failures=0,
                task_id="ui-task",
                session_id="session-id",
                steps=[
                    SimpleNamespace(success=True, error=None),
                ],
            )

    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: _FakeAdapter())
    monkeypatch.setattr("hermes_live_ui_controller.web_server.LiveWebUIRunner", _Runner)

    response = client.post("/api/runs", json={"manifest": _build_task()})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    for _ in range(10):
        run_state = client.get(f"/api/runs/{run_id}").json()
        if run_state["status"] != "queued":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("run did not finish in expected timeframe")

    assert run_state["status"] in {"done", "failed"}
