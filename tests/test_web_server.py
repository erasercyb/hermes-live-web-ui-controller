from __future__ import annotations

import json
import tempfile
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermes_live_ui_controller.models import RunTask, SnapshotState
from hermes_live_ui_controller.web_server import build_app


def _temp_data_dir() -> str:
    return tempfile.mkdtemp(prefix="hermes-web-ui-test-")


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
    client = TestClient(build_app(_temp_data_dir()))
    response = client.get("/")
    assert response.status_code == 200
    assert "Hermes Live UI Controller" in response.text


def test_open_session_from_project(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))
    fake_adapter = _FakeAdapter()
    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: fake_adapter)

    created = client.post("/api/projects", json={"name": "Demo", "url": "https://example.com"})
    assert created.status_code == 200
    project = created.json()

    opened = client.post(f"/api/sessions/from-project/{project['id']}")
    assert opened.status_code == 200
    opened_payload = opened.json()
    assert opened_payload["project_id"] == project["id"]
    assert opened_payload["session"]["project_id"] == project["id"]


def test_open_session_from_registry(monkeypatch, tmp_path) -> None:
    client = TestClient(build_app(_temp_data_dir()))
    fake_adapter = _FakeAdapter()
    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: fake_adapter)

    registry = {
        "projects": [
            {
                "slug": "demo-registry",
                "name": "Demo Registry",
                "status": "active",
                "profiles": ["hermes"],
                "paths": ["https://registry-demo.example.com"],
            }
        ]
    }
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(registry), encoding="utf-8")

    opened = client.post(
        "/api/sessions/from-registry",
        json={"slug": "demo-registry", "path": str(registry_file)},
    )
    assert opened.status_code == 200
    opened_payload = opened.json()
    assert opened_payload["project_id"] == "demo-registry"
    assert opened_payload["session"]["project_id"] == "demo-registry"


def test_import_projects_from_registry(tmp_path) -> None:
    registry = {
        "projects": [
            {
                "slug": "demo-one",
                "name": "Demo One",
                "status": "planned",
                "profiles": ["hermes"],
                "paths": ["https://example.com", "/tmp/local-path"],
                "group_slug": "demo",
                "group_name": "Demo Group",
            },
            {
                "slug": "demo-two",
                "name": "Demo Two",
                "status": "completed",
                "profiles": ["hermes"],
                "paths": ["https://example-two.com"],
                "group_slug": "demo",
                "group_name": "Demo Group",
            },
        ]
    }
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(registry), encoding="utf-8")

    client = TestClient(build_app(_temp_data_dir()))

    response = client.post(
        "/api/projects/import-registry",
        json={
            "path": str(registry_file),
            "max_items": 1,
            "status_filter": ["planned"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["added"] == 1
    assert payload["skipped_urls"] == 0
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["url"] == "https://example.com"

    response = client.post(
        "/api/projects/import-registry",
        json={
            "path": str(registry_file),
            "status_filter": ["completed"],
        },
    )
    assert response.status_code == 200
    second = response.json()
    assert second["added"] == 1
    assert second["skipped_urls"] == 0


def test_import_registry_invalid_path() -> None:
    client = TestClient(build_app(_temp_data_dir()))
    response = client.post(
        "/api/projects/import-registry",
        json={"path": "/tmp/does-not-exist.json"},
    )
    assert response.status_code == 400


def test_live_session_and_action_flow(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))
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


def test_session_verify_endpoint(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))
    fake_adapter = _FakeAdapter()
    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: fake_adapter)

    response = client.post("/api/sessions", json={"url": "https://example.com"})
    assert response.status_code == 200
    session_id = response.json()["id"]

    passed = client.post(
        f"/api/sessions/{session_id}/verify",
        json={"must_contain_text": ["start"], "must_have_ref": ["btn-save"]},
    )
    assert passed.status_code == 200
    payload = passed.json()
    assert payload["passed"] is True

    not_passed = client.post(
        f"/api/sessions/{session_id}/verify",
        json={"must_contain_text": ["does-not-exist"], "must_have_ref": ["btn-save"]},
    )
    assert not_passed.status_code == 200
    payload = not_passed.json()
    assert payload["passed"] is False
    assert len(payload["failures"]) >= 1


@pytest.mark.parametrize(
    "complexity,expected_status",
    [("complex", "waiting_subagents"), ("critical", "waiting_subagents"), ("simple", "todo")],
)
def test_kanban_status_from_complexity(complexity: str, expected_status: str) -> None:
    client = TestClient(build_app(_temp_data_dir()))
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
    client = TestClient(build_app(_temp_data_dir()))
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
    assert updated["handoff_payload"]["source_task_id"] == task["id"]

    handoffs = client.get("/api/kanban/handoffs").json()
    assert len(handoffs) == 1
    assert handoffs[0]["source_task_id"] == task["id"]

    # wiederholter Handoff ersetzt statt zu duplizieren
    handoff_repeat = client.post(f"/api/kanban/{task['id']}/handoff")
    assert handoff_repeat.status_code == 200

    handoffs = client.get("/api/kanban/handoffs").json()
    assert len(handoffs) == 1


def test_complex_kanban_auto_handoff() -> None:
    client = TestClient(build_app(_temp_data_dir()))

    response = client.post(
        "/api/kanban",
        json={
            "title": "Komplexe Änderung",
            "description": "Cross-Feature Update",
            "complexity": "complex",
        },
    )
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "waiting_subagents"
    assert task["handoff_payload"]["external_kanban_status"] == "not_configured"

    handoffs = client.get("/api/kanban/handoffs").json()
    assert len(handoffs) == 1
    assert handoffs[0]["source_task_id"] == task["id"]


def test_complex_kanban_auto_handoff_creates_external_task(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"id":"kb_ext_1","title":"Komplexe Änderung"}', stderr="")

    client = TestClient(build_app(_temp_data_dir()))
    monkeypatch.setenv("LIVE_UI_KANBAN_CMD", "1")
    monkeypatch.setenv("LIVE_UI_KANBAN_ASSIGNEE", "hermes-live-agent")
    monkeypatch.setattr("hermes_live_ui_controller.web_server.subprocess.run", _fake_run)

    response = client.post(
        "/api/kanban",
        json={
            "title": "Komplexe Änderung",
            "description": "Cross-Feature Update",
            "complexity": "complex",
        },
    )
    assert response.status_code == 200
    task = response.json()

    handoff = task["handoff_payload"]
    assert handoff["external_kanban_status"] == "created"
    assert handoff["external_kanban"] == "kb_ext_1"
    assert len(calls) == 1
    assert calls[0][0:2] == ["hermes", "kanban"]
    assert calls[0][2] == "create"
    assert calls[0][3] == task["title"]
    assert "--json" in calls[0]


def test_critical_kanban_auto_handoff_creates_external_task(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"id":"kb_ext_2","title":"Kritische Änderung"}', stderr="")

    client = TestClient(build_app(_temp_data_dir()))
    monkeypatch.setenv("LIVE_UI_KANBAN_CMD", "1")
    monkeypatch.setattr("hermes_live_ui_controller.web_server.subprocess.run", _fake_run)

    response = client.post(
        "/api/kanban",
        json={
            "title": "Kritische Änderung",
            "description": "Feature mit hohem Risiko",
            "complexity": "critical",
        },
    )
    assert response.status_code == 200
    task = response.json()

    handoff = task["handoff_payload"]
    assert handoff["external_kanban_status"] == "created"
    assert handoff["external_kanban"] == "kb_ext_2"
    assert len(calls) == 1
    assert calls[0][3] == task["title"]


def test_session_action_history(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))
    fake_adapter = _FakeAdapter()
    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: fake_adapter)

    opened = client.post(
        "/api/sessions",
        json={"url": "https://example.com"},
    )
    assert opened.status_code == 200
    session = opened.json()
    sid = session["id"]

    action = client.post(
        f"/api/sessions/{sid}/action",
        json={"type": "click", "ref": "btn-save"},
    )
    assert action.status_code == 200
    assert action.json()["ok"] is True

    history = client.get(f"/api/sessions/{sid}/history").json()
    assert len(history) == 1
    assert history[0]["type"] == "click"
    assert history[0]["ok"] is True


def test_run_api_async_success(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))

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


def test_dashboard_stats_endpoint(monkeypatch) -> None:
    client = TestClient(build_app(_temp_data_dir()))

    monkeypatch.setattr("hermes_live_ui_controller.web_server.build_adapter", lambda *_args, **_kwargs: _FakeAdapter())

    created = client.post(
        "/api/projects",
        json={"name": "Demo", "url": "https://example.com"},
    )
    assert created.status_code == 200

    task = client.post(
        "/api/kanban",
        json={"title": "Statik prüfen", "description": "Check endpoint", "complexity": "simple"},
    )
    assert task.status_code == 200

    session = client.post("/api/sessions", json={"url": "https://example.com"})
    assert session.status_code == 200

    stats = client.get("/api/stats").json()
    assert stats["projects_count"] == 1
    assert "session_action_count" in stats
    assert stats["kanban_count"] >= 1
