"""Web UI for live UI tasks and project-level Kanban planning."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .controller import LiveWebUIRunner
from .models import RunTask, SnapshotState, StepAction
from .runtime import AdapterConfig, build_adapter

try:  # pragma: no cover
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError(
        "FastAPI ist nicht installiert. Installiere mit: pip install hermes-live-web-ui-controller[web]"
    ) from exc

try:  # pragma: no cover
    from fastapi import status
    from fastapi.responses import HTMLResponse
except Exception:  # pragma: no cover
    status = None
    HTMLResponse = None

DEFAULT_STATUS = {"todo", "in_progress", "waiting_subagents", "done", "blocked"}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _snapshot_to_dict(snapshot: SnapshotState) -> dict[str, Any]:
    return {
        "ref_ids": snapshot.ref_ids,
        "text": snapshot.text,
        "title": snapshot.title,
        "url": snapshot.url,
        "console_errors": snapshot.console_errors,
        "meta": snapshot.meta,
    }


def _action_to_step(action: ActionRequest) -> StepAction:
    return StepAction(
        type=action.type,
        ref=action.ref,
        url=action.url,
        text=action.text,
        direction=action.direction,
        amount=action.amount,
        key=action.key,
        require_confirmation=action.require_confirmation,
    )


def _read_json(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    import json

    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


class _DataStore:
    """Minimal persistierte Datenhaltung für Projekte, Runs und Kanban-Karten."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta = self.root / "meta"
        self.meta.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        self.projects_path = self.meta / "projects.json"
        self.kanban_path = self.meta / "kanban.json"

    def projects(self) -> list[dict[str, Any]]:
        return _read_json(self.projects_path) if self.projects_path.exists() else []

    def save_projects(self, items: list[dict[str, Any]]) -> None:
        _write_json(self.projects_path, items)

    def kanban(self) -> list[dict[str, Any]]:
        return _read_json(self.kanban_path) if self.kanban_path.exists() else []

    def save_kanban(self, items: list[dict[str, Any]]) -> None:
        _write_json(self.kanban_path, items)


class Session:
    def __init__(self, session_id: str, project_id: str | None, url: str, adapter: Any) -> None:
        self.session_id = session_id
        self.project_id = project_id
        self.url = url
        self.adapter = adapter
        self.created_at = _now()
        self.last_seen = self.created_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "project_id": self.project_id,
            "url": self.url,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.as_dict() for session in self._sessions.values()]

    def create(self, project_id: str | None, url: str, adapter: Any) -> str:
        session_id = _new_id("ws")
        with self._lock:
            self._sessions[session_id] = Session(
                session_id=session_id,
                project_id=project_id,
                url=url,
                adapter=adapter,
            )
        return session_id

    def get(self, session_id: str) -> Session:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(session_id)
            session = self._sessions[session_id]
            session.last_seen = _now()
            return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id)
        close = getattr(session.adapter, "close", None)
        if callable(close):
            close()


class RunJobStore:
    """In-Memory-Laufprotokoll für asynchrone Manifest-Ausführungen."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[payload["id"]] = payload

    def update(self, run_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs[run_id]
            record.update(payload)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(run_id)


class ProjectRequest(BaseModel):
    name: str
    url: str


class SessionCreateRequest(BaseModel):
    url: str
    project_id: str | None = None
    no_headless: bool = False
    slow_mo_ms: int = 0


class ActionRequest(BaseModel):
    type: str
    ref: str | None = None
    url: str | None = None
    text: str | None = None
    direction: str | None = None
    amount: int = 1
    key: str | None = None
    require_confirmation: bool = False


class RunRequest(BaseModel):
    manifest: dict[str, Any]
    adapter: str = "playwright"
    mock_pages: str | None = None
    no_headless: bool = False
    slow_mo_ms: int = 0
    auto_confirm: bool = False


class KanbanRequest(BaseModel):
    title: str
    description: str
    project: str | None = None
    priority: str = "normal"
    status: str = "todo"
    complexity: str = "simple"


class KanbanUpdateRequest(BaseModel):
    status: str | None = None
    notes: str | None = None
    assignee: str | None = None


def build_app(data_dir: str | None = None) -> FastAPI:
    root = Path(data_dir) if data_dir else Path.cwd() / ".hermes-live-web-ui"
    store = _DataStore(root)
    sessions = SessionManager()
    runs = RunJobStore()

    app = FastAPI(title="Hermes Live UI Controller")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:  # pragma: no cover - UI smoke is validated via static response test
        return _dashboard_html()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "ts": _now()}

    @app.get("/api/projects")
    def get_projects() -> list[dict[str, Any]]:
        return store.projects()

    @app.post("/api/projects")
    def post_project(payload: ProjectRequest) -> dict[str, Any]:
        projects = store.projects()
        project = payload.model_dump()
        project["id"] = _new_id("pr")
        project["created_at"] = _now()
        projects.append(project)
        store.save_projects(projects)
        return project

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, str]:
        projects = [item for item in store.projects() if item["id"] != project_id]
        store.save_projects(projects)
        return {"status": "deleted"}

    @app.get("/api/sessions")
    def get_sessions() -> list[dict[str, Any]]:
        return sessions.list_sessions()

    @app.post("/api/sessions")
    def open_session(payload: SessionCreateRequest) -> dict[str, Any]:
        run_task = RunTask(task_id=_new_id("task"), goal="Interactive session", url=payload.url, steps=[])
        config = AdapterConfig(
            mode="playwright",
            no_headless=payload.no_headless,
            slow_mo_ms=payload.slow_mo_ms,
        )
        try:
            adapter = build_adapter(run_task, config)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Session konnte nicht gestartet werden: {exc}") from exc

        session_id = sessions.create(payload.project_id, payload.url, adapter)
        session = sessions.get(session_id)
        snapshot = _snapshot_to_dict(adapter.snapshot(include_full=True))
        return {
            "id": session_id,
            "project_id": session.project_id,
            "session": session.as_dict(),
            "snapshot": snapshot,
        }

    @app.get("/api/sessions/{session_id}/snapshot")
    def get_session_snapshot(session_id: str) -> dict[str, Any]:
        try:
            session = sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden") from exc

        snapshot = session.adapter.snapshot(include_full=True)
        return _snapshot_to_dict(snapshot)

    @app.post("/api/sessions/{session_id}/action")
    def post_session_action(session_id: str, payload: ActionRequest) -> dict[str, Any]:
        try:
            session = sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden") from exc

        action = _action_to_step(payload)
        before = session.adapter.snapshot(include_full=True)
        try:
            if action.type == "navigate":
                session.adapter.navigate(action.url or "")
            elif action.type == "click":
                session.adapter.click(action.ref or "")
            elif action.type == "type":
                session.adapter.type(action.ref or "", action.text or "")
            elif action.type == "scroll":
                direction = action.direction.value if action.direction else (payload.direction or "down")
                session.adapter.scroll(direction, action.amount or 1)
            elif action.type == "press":
                session.adapter.press(action.key or "")
            elif action.type == "back":
                session.adapter.back()
            elif action.type == "snapshot":
                pass
            else:
                raise HTTPException(status_code=400, detail=f"Unbekannter Action-Typ: {action.type}")

            after = session.adapter.snapshot(include_full=True)
            console_events = session.adapter.console(clear=True)
            return {
                "ok": True,
                "snapshot_before": _snapshot_to_dict(before),
                "snapshot_after": _snapshot_to_dict(after),
                "console_events": console_events,
            }
        except Exception as exc:
            after = session.adapter.snapshot(include_full=True)
            return {
                "ok": False,
                "error": str(exc),
                "snapshot_before": _snapshot_to_dict(before),
                "snapshot_after": _snapshot_to_dict(after),
            }

    @app.delete("/api/sessions/{session_id}")
    def close_session(session_id: str) -> dict[str, str]:
        try:
            sessions.close(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden") from exc
        return {"status": "closed"}

    @app.post("/api/runs", status_code=202)
    def start_run(payload: RunRequest) -> dict[str, str]:
        try:
            manifest = RunTask.model_validate(payload.manifest)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Ungültiger Task: {exc}") from exc

        run_id = _new_id("run")
        run_record = {
            "id": run_id,
            "status": "queued",
            "task_id": manifest.task_id,
            "created_at": _now(),
            "updated_at": _now(),
            "result": None,
            "error": None,
        }
        runs.create(run_record)

        adapter_conf = AdapterConfig(
            mode=payload.adapter,
            mock_pages=payload.mock_pages,
            no_headless=payload.no_headless,
            slow_mo_ms=payload.slow_mo_ms,
        )

        def execute_run() -> None:
            runs.update(run_id, {"status": "running", "updated_at": _now()})
            adapter = None
            try:
                adapter = build_adapter(manifest, adapter_conf)
                runner = LiveWebUIRunner(
                    adapter=adapter,
                    auto_confirm=payload.auto_confirm,
                    output_dir=str(store.runs_dir),
                )
                result = runner.run(manifest)
                runs.update(
                    run_id,
                    {
                        "status": "done" if result.success else "failed",
                        "updated_at": _now(),
                        "result": result.model_dump(mode="json"),
                    },
                )
            except Exception as exc:
                runs.update(
                    run_id,
                    {"status": "failed", "updated_at": _now(), "error": str(exc)},
                )
            finally:
                if adapter is not None:
                    close = getattr(adapter, "close", None)
                    if callable(close):
                        close()

        thread = threading.Thread(target=execute_run, daemon=True, name=f"live-run-{run_id}")
        thread.start()

        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/runs")
    def get_runs() -> list[dict[str, Any]]:
        return runs.list()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run nicht gefunden")
        return run

    @app.get("/api/kanban")
    def get_kanban() -> list[dict[str, Any]]:
        return store.kanban()

    @app.post("/api/kanban")
    def post_kanban(payload: KanbanRequest) -> dict[str, Any]:
        tasks = store.kanban()
        task = payload.model_dump()
        task["id"] = _new_id("kb")
        task["created_at"] = _now()
        task["updated_at"] = _now()
        task["notes"] = ""
        task["assignee"] = ""

        if task["complexity"] == "complex":
            task["status"] = "waiting_subagents"
        elif task["status"] not in DEFAULT_STATUS:
            task["status"] = "todo"

        tasks.append(task)
        store.save_kanban(tasks)
        return task

    @app.patch("/api/kanban/{task_id}")
    def patch_kanban(task_id: str, payload: KanbanUpdateRequest) -> dict[str, Any]:
        tasks = store.kanban()
        for task in tasks:
            if task["id"] == task_id:
                for key, value in payload.model_dump().items():
                    if value is not None:
                        task[key] = value
                task["updated_at"] = _now()
                store.save_kanban(tasks)
                return task

        raise HTTPException(status_code=404, detail="Kanban-Aufgabe nicht gefunden")

    return app


def run_server(host: str = "127.0.0.1", port: int = 8765, data_dir: str | None = None) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "Uvicorn ist nicht installiert. Installiere mit: pip install hermes-live-web-ui-controller[web]"
        ) from exc

    uvicorn.run(build_app(data_dir=data_dir), host=host, port=port)


def _dashboard_html() -> str:
    return """
<!doctype html>
<html lang=\"de\">
  <head>
    <meta charset=\"utf-8\" />
    <title>Hermes Live UI Controller</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 1.5rem; background: #f8fafc; color: #0f172a; }
      h1 { margin-top: 0; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
      section { background: #fff; border: 1px solid #d4dce8; border-radius: .7rem; padding: 1rem; }
      pre { max-height: 280px; overflow: auto; background: #0f172a; color: #e2e8f0; padding: .8rem; }
      label { display: block; margin-bottom: .6rem; }
      input, select, textarea, button { width: 100%; margin-top: .2rem; padding: .45rem; }
      button { margin-top: .5rem; }
      small { color: #64748b; }
    </style>
  </head>
  <body>
    <h1>Hermes Live UI Controller</h1>
    <small>
      Projekt-Web-UI laden, Interaktionen ausführen, Task-Manifeste starten,
      Kanban mit Subagent-Handoff planen.
    </small>

    <div class="grid">
      <section>
        <h2>Projekt registrieren</h2>
        <label>Name<input id="project_name" /></label>
        <label>URL<input id="project_url" placeholder="https://..." /></label>
        <button onclick="createProject()">Projekt speichern</button>
        <pre id="projects">Lade...</pre>
      </section>

      <section>
        <h2>Live-Session</h2>
        <label>Session-URL<input id="session_url" /></label>
        <label><input id="no_headless" type="checkbox" /> Headless deaktivieren</label>
        <button onclick="openSession()">Session starten</button>
        <label>Action</label>
        <select id="action_type">
          <option>navigate</option>
          <option>click</option>
          <option>type</option>
          <option>scroll</option>
          <option>press</option>
          <option>back</option>
          <option>snapshot</option>
        </select>
        <label>Ref<input id="action_ref" /></label>
        <label>Wert<input id="action_text" /></label>
        <button onclick="runAction()">Action senden</button>
        <pre id="session_state">Session nicht aktiv</pre>
      </section>

      <section>
        <h2>Run starten</h2>
        <label>Manifest JSON</label>
        <textarea id="manifest" rows="10">
{
  "task_id": "my-task",
  "goal": "example",
  "url": "https://example.com",
  "steps": []
}
</textarea>
        <label>Adapter
          <select id="run_adapter">
            <option>playwright</option>
            <option>mock</option>
            <option>none</option>
          </select>
        </label>
        <button onclick="startRun()">Run starten</button>
        <pre id="run_status">---</pre>
      </section>

      <section>
        <h2>Kanban</h2>
        <label>Titel<input id="kb_title" /></label>
        <label>Beschreibung<textarea id="kb_desc" rows="5"></textarea></label>
        <label>Komplexität
          <select id="kb_complexity">
            <option>simple</option>
            <option>complex</option>
            <option>critical</option>
          </select>
        </label>
        <button onclick="createKanban()">Neue Karte</button>
        <pre id="kanban">Lade...</pre>
      </section>
    </div>

    <script>
      let currentSession = null;

      async function api(path, options = {}) {
        const response = await fetch(path, options);
        return await response.json();
      }

      async function refreshProjects() {
        const projects = await api('/api/projects');
        document.getElementById('projects').textContent = JSON.stringify(projects, null, 2);
      }

      async function refreshKanban() {
        const kanban = await api('/api/kanban');
        document.getElementById('kanban').textContent = JSON.stringify(kanban, null, 2);
      }

      async function createProject() {
        await api('/api/projects', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: document.getElementById('project_name').value,
            url: document.getElementById('project_url').value,
          }),
        });
        await refreshProjects();
      }

      async function openSession() {
        const result = await api('/api/sessions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: document.getElementById('session_url').value,
            no_headless: document.getElementById('no_headless').checked,
          }),
        });
        currentSession = result.id;
        document.getElementById('session_state').textContent = JSON.stringify(result, null, 2);
      }

      async function runAction() {
        if (!currentSession) {
          return;
        }
        const payload = {
          type: document.getElementById('action_type').value,
          ref: document.getElementById('action_ref').value || undefined,
          text: document.getElementById('action_text').value || undefined,
          url: document.getElementById('action_text').value || undefined,
          key: document.getElementById('action_text').value || undefined,
          amount: 1,
        };
        const result = await api(`/api/sessions/${currentSession}/action`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        document.getElementById('session_state').textContent = JSON.stringify(result, null, 2);
      }

      async function startRun() {
        const body = JSON.parse(document.getElementById('manifest').value);
        const result = await api('/api/runs', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            manifest: body,
            adapter: document.getElementById('run_adapter').value,
          }),
        });
        document.getElementById('run_status').textContent = JSON.stringify(result, null, 2);
      }

      async function createKanban() {
        await api('/api/kanban', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            title: document.getElementById('kb_title').value,
            description: document.getElementById('kb_desc').value,
            complexity: document.getElementById('kb_complexity').value,
          }),
        });
        await refreshKanban();
      }

      refreshProjects();
      refreshKanban();
    </script>
  </body>
</html>
"""
