"""Web UI for live UI tasks and project-level Kanban planning."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .controller import LiveWebUIRunner
from .models import CheckConfig, RunTask, SnapshotState, StepAction
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


def _is_complexity_needing_subagents(complexity: str) -> bool:
    return complexity.strip().lower() in {"complex", "critical"}


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


def _build_handoff_payload(task: dict[str, Any], scope: str = "hermes_live_ui_controller") -> dict[str, Any]:
    return {
        "handoff_id": _new_id("handoff"),
        "scope": scope,
        "goal": task.get("title", "").strip(),
        "description": task.get("description", "").strip(),
        "project": task.get("project"),
        "complexity": task.get("complexity", "complex"),
        "created_at": _now(),
        "status": "queued",
        "source_task_id": task.get("id"),
        "external_kanban": None,
        "external_kanban_status": "not_created",
    }


def _maybe_create_kanban_task(task: dict[str, Any], scope: str = "hermes_live_ui_controller") -> dict[str, Any]:
    """Optionaler Versuch, eine Hermes-Kanban-Aufgabe zu erzeugen.

    Dies wird nur ausgeführt, wenn die Umgebung `LIVE_UI_KANBAN_CMD=1` enthält.
    Der Rückgabewert wird im handoff-Payload hinterlegt, damit das Frontend und
    weitere Workflows den externen Status nachverfolgen können.
    """

    if os.getenv("LIVE_UI_KANBAN_CMD") != "1":
        return {"status": "not_configured"}

    payload = {
        "task_id": task.get("id", ""),
        "goal": task.get("title", ""),
        "description": task.get("description", ""),
        "project": task.get("project"),
        "complexity": task.get("complexity", "complex"),
        "scope": scope,
    }

    body = json.dumps(payload, ensure_ascii=False)
    args = ["hermes", "kanban", "create", payload["goal"], "--json", "--body", body]
    assignee = os.getenv("LIVE_UI_KANBAN_ASSIGNEE")
    if assignee:
        args.extend(["--assignee", assignee])

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return {"status": "hermes_not_found"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}

    if completed.returncode != 0:
        return {
            "status": "command_failed",
            "output": (completed.stdout or completed.stderr or "").strip()[:2000],
        }

    result = {"status": "created"}
    try:
        kanban = json.loads(completed.stdout)
        result["id"] = kanban.get("id") or kanban.get("task", {}).get("id")
        result["title"] = kanban.get("title") if isinstance(kanban, dict) else None
        result["output"] = completed.stdout
        return result
    except Exception:
        return {
            "status": "parse_failed",
            "output": (completed.stdout or completed.stderr or "").strip()[:2000],
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _extract_registry_project_urls(export_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project-Auswahl aus einem Registry-Export aufbereiten.

    Für das Live-WebUI-Setup werden nur HTTP/HTTPS-Pfade als öffnende URLs
    übernommen; lokale Pfade werden ignoriert, weil sie nicht direkt als Web-
    Oberfläche laden werden können.
    """

    candidates: list[dict[str, Any]] = []
    for item in export_payload.get("projects", []) or []:
        paths = item.get("paths", []) or []
        urls = [path for path in paths if isinstance(path, str) and path.startswith(("http://", "https://"))]
        if not urls:
            continue

        candidates.append(
            {
                "slug": item.get("slug"),
                "name": item.get("name", item.get("slug")),
                "status": item.get("status", "unknown"),
                "group_slug": item.get("group_slug"),
                "group_name": item.get("group_name"),
                "urls": urls,
                "profiles": item.get("profiles", []),
            }
        )

    return candidates


def _load_registry_candidates(registry_path: str) -> list[dict[str, Any]]:
    payload = _read_json(Path(registry_path))
    if not isinstance(payload, dict):
        raise ValueError("Registry-Datei ist kein JSON-Objekt")
    return _extract_registry_project_urls(payload)


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
        self.handoffs_path = self.meta / "handoffs.json"

    def projects(self) -> list[dict[str, Any]]:
        return _read_json(self.projects_path) if self.projects_path.exists() else []

    def save_projects(self, items: list[dict[str, Any]]) -> None:
        _write_json(self.projects_path, items)

    def kanban(self) -> list[dict[str, Any]]:
        return _read_json(self.kanban_path) if self.kanban_path.exists() else []

    def save_kanban(self, items: list[dict[str, Any]]) -> None:
        _write_json(self.kanban_path, items)

    def handoffs(self) -> list[dict[str, Any]]:
        return _read_json(self.handoffs_path) if self.handoffs_path.exists() else []

    def add_handoff(self, item: dict[str, Any]) -> None:
        items = self.handoffs()
        source_task_id = item.get("source_task_id")
        if source_task_id:
            items = [it for it in items if it.get("source_task_id") != source_task_id]

        items.append(item)
        _write_json(self.handoffs_path, items)


class Session:
    def __init__(self, session_id: str, project_id: str | None, url: str, adapter: Any) -> None:
        self.session_id = session_id
        self.project_id = project_id
        self.url = url
        self.adapter = adapter
        self.created_at = _now()
        self.last_seen = self.created_at
        self.action_history: list[dict[str, Any]] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "project_id": self.project_id,
            "url": self.url,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "action_count": len(self.action_history),
        }

    def append_action(self, item: dict[str, Any]) -> None:
        self.action_history.append(item)
        if len(self.action_history) > 200:
            self.action_history = self.action_history[-200:]


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


class SessionVerifyRequest(BaseModel):
    must_have_ref: list[str] = []
    must_contain_text: list[str] = []
    console_no_fatal: bool = True
    max_console_errors: int = 0
    clear_console: bool = True


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


class RegistryImportRequest(BaseModel):
    path: str = "/root/project-registry/exports/projects.json"
    max_items: int | None = 200
    status_filter: list[str] = []
    include_profiles: list[str] = []


class RegistryOpenRequest(BaseModel):
    slug: str
    path: str = "/root/project-registry/exports/projects.json"
    pick_url: str | None = None
    status_filter: list[str] = []
    include_profiles: list[str] = []


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

    @app.post("/api/projects/import-registry")
    def import_projects_from_registry(payload: RegistryImportRequest) -> dict[str, Any]:
        try:
            candidates = _load_registry_candidates(payload.path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Registry-Import fehlgeschlagen: {exc}") from exc

        if payload.status_filter:
            status_filter = {item.lower() for item in payload.status_filter}
            candidates = [c for c in candidates if str(c.get("status", "")).lower() in status_filter]

        if payload.include_profiles:
            include_profiles = set(payload.include_profiles)
            candidates = [c for c in candidates if set(c.get("profiles", [])).intersection(include_profiles)]

        projects = store.projects()
        existing_urls = {item["url"] for item in projects}
        added: list[dict[str, Any]] = []
        skipped_urls = 0
        remaining = payload.max_items

        for candidate in candidates:
            for index, url in enumerate(candidate.get("urls", []), start=1):
                if remaining is not None and remaining <= 0:
                    break

                if url in existing_urls:
                    skipped_urls += 1
                    continue

                name = candidate.get("name") or candidate.get("slug") or url
                if len(candidate.get("urls", [])) > 1:
                    name = f"{name} ({candidate.get('slug', '')} #{index})"

                project = {
                    "id": _new_id("pr"),
                    "name": name,
                    "url": url,
                    "slug": candidate.get("slug"),
                    "status": candidate.get("status"),
                    "group_slug": candidate.get("group_slug"),
                    "group_name": candidate.get("group_name"),
                    "profiles": candidate.get("profiles", []),
                    "created_at": _now(),
                    "source": "registry-import",
                }

                projects.append(project)
                existing_urls.add(url)
                added.append(project)

                if remaining is not None:
                    remaining -= 1

        store.save_projects(projects)

        return {
            "path": payload.path,
            "scanned": len(candidates),
            "added": len(added),
            "skipped_urls": skipped_urls,
            "projects": added,
        }

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, str]:
        projects = [item for item in store.projects() if item["id"] != project_id]
        store.save_projects(projects)
        return {"status": "deleted"}

    @app.get("/api/sessions")
    def get_sessions() -> list[dict[str, Any]]:
        return sessions.list_sessions()

    def _open_session(project_id: str | None, payload: SessionCreateRequest) -> dict[str, Any]:
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

        session_id = sessions.create(project_id, payload.url, adapter)
        session = sessions.get(session_id)
        snapshot = _snapshot_to_dict(adapter.snapshot(include_full=True))
        return {
            "id": session_id,
            "project_id": session.project_id,
            "session": session.as_dict(),
            "snapshot": snapshot,
        }

    @app.post("/api/sessions")
    def open_session(payload: SessionCreateRequest) -> dict[str, Any]:
        return _open_session(payload.project_id, payload)

    @app.post("/api/sessions/from-registry")
    def open_session_from_registry(payload: RegistryOpenRequest) -> dict[str, Any]:
        try:
            candidates = _load_registry_candidates(payload.path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Registry-Import fehlgeschlagen: {exc}") from exc

        if payload.status_filter:
            status_filter = {item.lower() for item in payload.status_filter}
            candidates = [item for item in candidates if str(item.get("status", "")).lower() in status_filter]

        if payload.include_profiles:
            include_profiles = set(payload.include_profiles)
            candidates = [item for item in candidates if set(item.get("profiles", [])).intersection(include_profiles)]

        slug = payload.slug.strip().lower()
        slug_matches = [
            item
            for item in candidates
            if (item.get("slug") or "").lower() == slug or (item.get("name") or "").lower() == slug
        ]

        if not slug_matches:
            raise HTTPException(status_code=404, detail="Projekt-Slug nicht gefunden")

        candidate = slug_matches[0]
        project_urls = candidate.get("urls", [])
        if not project_urls:
            raise HTTPException(status_code=400, detail="Kein öffnender URL-Eintrag im Projekt")

        target_url = payload.pick_url if payload.pick_url in project_urls else project_urls[0]
        return _open_session(
            candidate.get("slug"),
            SessionCreateRequest(url=target_url),
        )

    @app.post("/api/sessions/from-project/{project_id}")
    def open_session_from_project(project_id: str) -> dict[str, Any]:
        projects = store.projects()
        project = next((item for item in projects if item["id"] == project_id), None)
        if project is None:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")

        payload = SessionCreateRequest(url=project["url"], project_id=project_id)
        return _open_session(project_id, payload)

    @app.get("/api/sessions/{session_id}/history")
    def get_session_history(session_id: str) -> list[dict[str, Any]]:
        try:
            session = sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden") from exc
        return list(session.action_history)

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
        started_at = _now()
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
            entry = {
                "id": _new_id("act"),
                "started_at": started_at,
                "finished_at": _now(),
                "type": action.type,
                "ok": True,
                "payload": payload.model_dump(),
            }
            session.append_action(entry)
            return {
                "ok": True,
                "snapshot_before": _snapshot_to_dict(before),
                "snapshot_after": _snapshot_to_dict(after),
                "console_events": console_events,
            }
        except Exception as exc:
            after = session.adapter.snapshot(include_full=True)
            entry = {
                "id": _new_id("act"),
                "started_at": started_at,
                "finished_at": _now(),
                "type": action.type,
                "ok": False,
                "payload": payload.model_dump(),
                "error": str(exc),
            }
            session.append_action(entry)
            return {
                "ok": False,
                "error": str(exc),
                "snapshot_before": _snapshot_to_dict(before),
                "snapshot_after": _snapshot_to_dict(after),
                "action_log_entry": entry,
            }

    @app.post("/api/sessions/{session_id}/verify")
    def verify_session(session_id: str, payload: SessionVerifyRequest) -> dict[str, Any]:
        try:
            session = sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session nicht gefunden") from exc

        checks = CheckConfig.model_validate(payload.model_dump())
        snapshot = session.adapter.snapshot(include_full=True)
        console_events = session.adapter.console(clear=payload.clear_console)

        failures: list[dict[str, str]] = []
        missing_refs = [ref for ref in checks.must_have_ref if ref not in snapshot.ref_ids]
        if missing_refs:
            failures.append({"scope": "snapshot", "message": f"Fehlende Refs: {', '.join(missing_refs)}"})

        normalized_text = (snapshot.text or "").lower()
        missing_text = [text for text in checks.must_contain_text if text.lower() not in normalized_text]
        if missing_text:
            failures.append({"scope": "snapshot", "message": f"Fehlender Text: {', '.join(missing_text)}"})

        if checks.console_no_fatal:
            fatal_events = [entry for entry in console_events if "error" in str(entry).lower()]
            if len(fatal_events) > checks.max_console_errors:
                failures.append(
                    {"scope": "console", "message": f"Zu viele Fehler im Console-Output: {len(fatal_events)}"}
                )

        entry = {
            "id": _new_id("verify"),
            "started_at": _now(),
            "type": "verify",
            "ok": len(failures) == 0,
            "payload": checks.model_dump(),
            "failure_count": len(failures),
        }
        session.append_action(entry)

        return {
            "passed": len(failures) == 0,
            "failures": failures,
            "snapshot": _snapshot_to_dict(snapshot),
            "console_events": console_events,
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

    @app.get("/api/stats")
    def get_stats() -> dict[str, Any]:
        kanban = store.kanban()
        handoffs = store.handoffs()
        runs_store = runs.list()
        kanban_status_counts: dict[str, int] = {}
        for item in kanban:
            status = item.get("status", "todo")
            kanban_status_counts[status] = kanban_status_counts.get(status, 0) + 1

        run_status_counts: dict[str, int] = {}
        for item in runs_store:
            status = item.get("status", "queued")
            run_status_counts[status] = run_status_counts.get(status, 0) + 1

        total_session_actions = sum((session.get("action_count", 0) or 0) for session in sessions.list_sessions())

        return {
            "projects_count": len(store.projects()),
            "active_sessions": len(sessions.list_sessions()),
            "session_action_count": total_session_actions,
            "kanban_count": len(kanban),
            "kanban_status_counts": kanban_status_counts,
            "run_count": len(runs_store),
            "run_status_counts": run_status_counts,
            "handoff_count": len(handoffs),
            "handoff_status_counts": {
                "queued": sum(1 for item in handoffs if item.get("status") == "queued"),
                "done": sum(1 for item in handoffs if item.get("status") == "done"),
            },
            "storage_root": str(store.root),
            "generated_at": _now(),
        }

    @app.get("/api/kanban")
    def get_kanban() -> list[dict[str, Any]]:
        return store.kanban()

    @app.get("/api/kanban/handoffs")
    def get_handoffs() -> list[dict[str, Any]]:
        return store.handoffs()

    @app.post("/api/kanban")
    def post_kanban(payload: KanbanRequest) -> dict[str, Any]:
        tasks = store.kanban()
        task = payload.model_dump()
        task["id"] = _new_id("kb")
        task["created_at"] = _now()
        task["updated_at"] = _now()
        task["notes"] = ""
        task["assignee"] = ""

        if _is_complexity_needing_subagents(task["complexity"]):
            task["status"] = "waiting_subagents"
            handoff_payload = _build_handoff_payload(task)
            task["handoff_payload"] = handoff_payload
            external = _maybe_create_kanban_task(task)
            handoff_payload["external_kanban_status"] = external.get("status", "not_configured")
            handoff_payload["external_kanban"] = external.get("id")
            store.add_handoff(handoff_payload)
        elif task["status"] not in DEFAULT_STATUS:
            task["status"] = "todo"

        tasks.append(task)
        store.save_kanban(tasks)
        return task

    @app.post("/api/kanban/{task_id}/handoff")
    def handoff_kanban_task(task_id: str) -> dict[str, Any]:
        tasks = store.kanban()
        for task in tasks:
            if task["id"] == task_id:
                task["status"] = "waiting_subagents"
                task["updated_at"] = _now()
                payload = _build_handoff_payload(task)
                external = _maybe_create_kanban_task(task)
                payload["external_kanban_status"] = external.get("status", "not_configured")
                payload["external_kanban"] = external.get("id")
                task["handoff_payload"] = payload
                task["notes"] = "Subagent-Trigger erstellt: handoff angefordert."
                store.add_handoff(payload)
                store.save_kanban(tasks)
                return task

        raise HTTPException(status_code=404, detail="Kanban-Aufgabe nicht gefunden")

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
      .grid.two-col { grid-template-columns: 1fr 1fr; }
      .grid.three-col { grid-template-columns: 1fr 1fr 1fr; }
      section { background: #fff; border: 1px solid #d4dce8; border-radius: .7rem; padding: 1rem; }
      .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: .6rem; }
      .stat-card { border: 1px solid #e2e8f0; border-radius: .6rem; padding: .65rem; background: #f8fafc; }
      .stat-key { color: #334155; font-weight: 600; }
      .stat-value { margin-top: .15rem; font-size: 1.3rem; }
      pre { max-height: 280px; overflow: auto; background: #0f172a; color: #e2e8f0; padding: .8rem; }
      label { display: block; margin-bottom: .6rem; }
      input, select, textarea, button { width: 100%; margin-top: .2rem; padding: .45rem; }
      button { margin-top: .5rem; }
      .toolbar { display: flex; gap: .6rem; align-items: center; margin-top: .4rem; }
      .toolbar > button, .toolbar > select, .toolbar > input { width: auto; }
      .template-preview {
        margin-top: .7rem;
        border: 1px dashed #cbd5e1;
        border-radius: .6rem;
        padding: .6rem;
        background: #f8fafc;
      }
      .hint { font-size: .87rem; color: #475569; }
      .muted { color: #64748b; }
      small { color: #64748b; }
    </style>
  </head>
  <body>
    <h1>Hermes Live UI Controller</h1>
    <small>
      Projekt-Web-UI laden, Interaktionen ausführen, Task-Manifeste starten,
      Kanban mit Subagent-Handoff planen.
    </small>

    <style id="template_style"></style>

    <div class="grid three-col">
      <section>
        <h2>Backend-Status</h2>
        <div class="stat-grid" id="stats_container">
          <div class="stat-card">
            <div class="stat-key">Projekte</div>
            <div class="stat-value" id="stat_projects">-</div>
          </div>
          <div class="stat-card">
            <div class="stat-key">Sessions</div>
            <div class="stat-value" id="stat_sessions">-</div>
          </div>
          <div class="stat-card">
            <div class="stat-key">Runs</div>
            <div class="stat-value" id="stat_runs">-</div>
          </div>
        </div>
        <small id="stats_updated" class="muted">Noch keine Aktualisierung</small>
        <div class="template-preview" id="stats_detail">Weitere Daten werden geladen…</div>
      </section>

      <section>
        <h2>Webdesigner-Vorlagen</h2>
        <label>Template
          <select id="template_select" onchange="applyTemplate(this.value)"></select>
        </label>
        <div class="template-preview">
          <strong id="template_name">Nicht gesetzt</strong>
          <div id="template_desc" class="hint">Wähle eine Vorlage für UI-Elemente aus.</div>
        </div>
        <div class="toolbar">
          <button onclick="applyTemplate(document.getElementById('template_select').value)">Template anwenden</button>
          <button onclick="revertTemplate()">Zurück</button>
        </div>

        <label>Lokale Plug-in Erweiterungen (JSON)</label>
        <textarea id="plugin_json" rows="6" placeholder='[{"id":"analytics-pro","label":"Analytics-Pro","category":"quickstart","action":{"type":"navigate","url":"https://example.com"}}]'></textarea>
        <div class="toolbar">
          <button onclick="loadPluginPack()">Pack laden</button>
          <button onclick="clearPluginPack()">Plugins löschen</button>
        </div>
        <small class="hint">Neue UI-/Action-Presets werden lokal persistiert (keine Backendänderung).</small>
      </section>

      <section>
        <h2>Entwicklungsfluss</h2>
        <label>
          <input id="stepwise_mode" type="checkbox" checked />
          Schrittweise/Bestätigung vor jedem Schritt
        </label>
        <label>
          <input id="require_confirmation" type="checkbox" />
          Bestätige unbekannte Aktionen automatisch im UI
        </label>
        <label>
          <input id="auto_refresh" type="checkbox" checked />
          Statistiken & Status automatisch refreshen
        </label>
        <label>Action-Preset auswählen
          <select id="action_preset_select" onchange="loadActionPreset()"></select>
        </label>
        <button onclick="runPresetAction()">Preset ausführen</button>
        <pre id="action_preset_preview">Noch kein Preset</pre>
      </section>
      <section>
        <h2>Projekt registrieren</h2>
        <label>Name<input id="project_name" /></label>
        <label>URL<input id="project_url" placeholder="https://..." /></label>
        <button onclick="createProject()">Projekt speichern</button>
        <label>Gespeicherte Projekt-UI</label>
        <select id="project_select" onchange="syncSessionUrlFromProject(this.value)">
          <option value="">-- Projekt wählen --</option>
        </select>
        <button onclick="openProjectSession()">Projekt-Session öffnen</button>
        <pre id="projects">Lade...</pre>
      </section>

      <section>
        <h2>Projekt-Registry importieren</h2>
        <label>Registry-Datei
          <input id="registry_path" value="/root/project-registry/exports/projects.json" />
        </label>
        <label>Statusfilter (Komma getrennt)
          <input id="registry_status_filter" placeholder="planned,active,completed" />
        </label>
        <label>Max. neue URLs
          <input id="registry_max_items" type="number" min="1" step="1" value="20" />
        </label>
        <button onclick="importRegistry()">HTTP-URLs aus Registry importieren</button>
        <pre id="registry_import_result">Noch keine Registry-Aktion</pre>
      </section>

      <section>
        <h2>Projekt-Slug aus Registry öffnen</h2>
        <label>Slug<input id="registry_project_slug" placeholder="project-slug" /></label>
        <label>Registry-Datei
          <input id="registry_open_path" value="/root/project-registry/exports/projects.json" />
        </label>
        <label>URL (optional)
          <input id="registry_pick_url" placeholder="https://example.com" />
        </label>
        <label>URL-Filter (Komma getrennt)
          <input id="registry_open_status_filter" placeholder="planned,active" />
        </label>
        <button onclick="openRegistrySession()">Aus Registry via Slug laden</button>
        <pre id="registry_open_result">Noch kein Öffnen</pre>
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
        <label>Verify-Check</label>
        <textarea id="verify_payload" rows="5">
{"must_contain_text": [""], "must_have_ref": [], "max_console_errors": 0}
        </textarea>
        <button onclick="runVerify()">Session prüfen</button>
        <pre id="verify_result">Noch kein Verify</pre>
        <pre id="session_state">Session nicht aktiv</pre>
        <label>Letzte Action-Schritte</label>
        <pre id="session_history">Noch keine Aktionen</pre>
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

        <label>Handoff per Ticket-ID
          <input id="kb_handoff_id" placeholder="kb_..." />
        </label>
        <button onclick="handoffKanban()">Jetzt an Subagent geben</button>
        <pre id="handoffs">Lade...</pre>
      </section>
    </div>

    <script>
      let currentSession = null;

      async function api(path, options = {}) {
        const response = await fetch(path, options);
        return await response.json();
      }

      const STORAGE_KEY = 'live_ui_plugins_v1';
      const TEMPLATE_CATALOG = [
        {
          id: 'standard',
          name: 'Standard',
          description: 'Klares Basis-Layout mit Fokus auf Session- und Kanban-Blöcke.',
          css: '',
        },
        {
          id: 'designer',
          name: 'Designer',
          description: 'Dunkler Akzent, kompaktere Abstände – gut für lange Sessions.',
          css: [
            'body { background: #101f3a; color: #e2e8f0; }',
            'body button, body input, body select, body textarea { color: #0f172a; }',
            'section { background: #17233a; border-color: #334155; }',
            'pre { background: #020617; }',
            '.stat-card, .template-preview { background: #1e293b; }',
          ].join(' '),
        },
        {
          id: 'focus',
          name: 'Focus',
          description: 'Weniger Ablenkung mit größerem Kontrast und klarer Segmentierung.',
          css: [
            'body { background: #fef9c3; color: #3f3f46; }',
            'section { background: #fff; }',
            '.stat-card { background: #fff; }',
            '.template-preview { background: #fffbeb; }',
          ].join(' '),
        },
      ];

      const BUILTIN_PRESETS = [
        {
          id: 'snapshot',
          label: 'Snapshot',
          action: {type: 'snapshot'},
        },
        {
          id: 'scroll-down',
          label: 'Scroll Down',
          action: {type: 'scroll', direction: 'down', amount: 1},
        },
        {
          id: 'back',
          label: 'Back',
          action: {type: 'back'},
        },
      ];

      let activeTemplate = TEMPLATE_CATALOG[0];
      let actionPresets = [...BUILTIN_PRESETS];

      function storeSetPlugins(items) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(items || []));
      }

      function storeGetPlugins() {
        try {
          const raw = localStorage.getItem(STORAGE_KEY);
          return raw ? JSON.parse(raw) : [];
        } catch (_err) {
          return [];
        }
      }

      function renderTemplateSelect() {
        const select = document.getElementById('template_select');
        select.innerHTML = '';
        for (const template of TEMPLATE_CATALOG) {
          const option = document.createElement('option');
          option.value = template.id;
          option.textContent = template.name;
          select.appendChild(option);
        }
      }

      function syncTemplateView() {
        document.getElementById('template_name').textContent = activeTemplate.name;
        document.getElementById('template_desc').textContent = activeTemplate.description;
      }

      function applyTemplate(templateId) {
        const template = TEMPLATE_CATALOG.find((item) => item.id === templateId) || TEMPLATE_CATALOG[0];
        activeTemplate = template;
        const holder = document.getElementById('template_style');
        if (holder) {
          holder.textContent = template.css || '';
        }
        syncTemplateView();
        localStorage.setItem('live_ui_active_template_v1', template.id);
      }

      function revertTemplate() {
        const defaultTemplate = TEMPLATE_CATALOG[0];
        document.getElementById('template_select').value = defaultTemplate.id;
        applyTemplate(defaultTemplate.id);
      }

      function normalizePreset(preset, fallbackType = 'snapshot') {
        return {
          id: preset.id || `custom-${Math.random().toString(36).slice(2, 9)}`,
          label: preset.label || preset.id || 'Custom Preset',
          action: {
            type: preset.action?.type || fallbackType,
            ref: preset.action?.ref || null,
            url: preset.action?.url || null,
            text: preset.action?.text || null,
            direction: preset.action?.direction || null,
            amount: preset.action?.amount || 1,
            key: preset.action?.key || null,
            require_confirmation: !!preset.action?.require_confirmation,
          },
        };
      }

      function renderActionPresets() {
        const select = document.getElementById('action_preset_select');
        select.innerHTML = '';
        for (const preset of actionPresets) {
          const option = document.createElement('option');
          option.value = preset.id;
          option.textContent = preset.label;
          select.appendChild(option);
        }
        if (!select.value && actionPresets.length > 0) {
          select.value = actionPresets[0].id;
        }
        loadActionPreset();
      }

      function loadActionPreset() {
        const select = document.getElementById('action_preset_select');
        const preset = actionPresets.find((entry) => entry.id === select.value);
        if (!preset) {
          return;
        }
        const payload = preset.action;
        document.getElementById('action_preset_preview').textContent = JSON.stringify(payload, null, 2);
        document.getElementById('action_type').value = payload.type;
        document.getElementById('action_ref').value = payload.ref || '';
        document.getElementById('action_text').value = payload.text || payload.url || payload.key || '';
      }

      function loadPluginPack() {
        const raw = document.getElementById('plugin_json').value.trim();
        if (!raw) {
          return;
        }

        let parsed = [];
        try {
          parsed = JSON.parse(raw);
        } catch (_err) {
          alert('Ungültiges JSON für Plugin-Pack.');
          return;
        }
        if (!Array.isArray(parsed)) {
          alert('Plugin-Pack muss ein Array von Objekten sein.');
          return;
        }

        const pluginPresets = parsed
          .map((entry) => normalizePreset(entry))
          .filter((entry) => entry.id && entry.action?.type);
        if (!pluginPresets.length) {
          alert('Keine gültigen Presets gefunden.');
          return;
        }

        storeSetPlugins(pluginPresets);
        actionPresets = [...BUILTIN_PRESETS, ...pluginPresets];
        renderActionPresets();
        alert('Plugin-Pack geladen. Diese Presets sind sofort verfügbar.');
      }

      function clearPluginPack() {
        storeSetPlugins([]);
        actionPresets = [...BUILTIN_PRESETS];
        renderActionPresets();
      }

      function safeString(val) {
        return val == null ? '' : String(val);
      }

      async function refreshProjects() {
        const projects = await api('/api/projects');
        document.getElementById('projects').textContent = JSON.stringify(projects, null, 2);
        const select = document.getElementById('project_select');
        const prev = select.value;
        select.innerHTML = '<option value="">-- Projekt wählen --</option>';
        for (const project of projects) {
          const option = document.createElement('option');
          option.value = project.id;
          option.textContent = `${project.name} (${project.url})`;
          select.appendChild(option);
        }
        if (prev && projects.some((project) => project.id === prev)) {
          select.value = prev;
        }
      }

      function syncSessionUrlFromProject(projectId) {
        if (!projectId) {
          return;
        }
        const projects = document.getElementById('project_select');
        const chosen = Array.from(projects.options).find((option) => option.value === projectId);
        if (!chosen) {
          return;
        }
        const urlMatch = chosen.textContent.match(/\((.*)\)$/);
        if (urlMatch) {
          document.getElementById('session_url').value = urlMatch[1];
        }
      }

      async function importRegistry() {
        const statusFilterValue = document.getElementById('registry_status_filter').value || '';
        const statusFilter = statusFilterValue
          .split(',')
          .map((item) => item.trim())
          .filter((item) => item.length > 0);

        const maxItemsValue = parseInt(document.getElementById('registry_max_items').value || '20', 10);

        const result = await api('/api/projects/import-registry', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            path: document.getElementById('registry_path').value,
            max_items: Number.isNaN(maxItemsValue) ? null : maxItemsValue,
            status_filter: statusFilter,
            include_profiles: [],
          }),
        });
        document.getElementById('registry_import_result').textContent = JSON.stringify(result, null, 2);
        await refreshProjects();
      }

      async function refreshKanban() {
        const kanban = await api('/api/kanban');
        document.getElementById('kanban').textContent = JSON.stringify(kanban, null, 2);
      }

      async function refreshHandoffs() {
        const handoffs = await api('/api/kanban/handoffs');
        document.getElementById('handoffs').textContent = JSON.stringify(handoffs, null, 2);
      }

      async function refreshSessionHistory() {
        if (!currentSession) {
          return;
        }
        const history = await api(`/api/sessions/${currentSession}/history`);
        document.getElementById('session_history').textContent = JSON.stringify(history.slice(-20), null, 2);
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
        await refreshSessionHistory();
      }

      async function openProjectSession() {
        const projectId = document.getElementById('project_select').value;
        if (!projectId) {
          return;
        }
        const result = await api(`/api/sessions/from-project/${projectId}`, {
          method: 'POST',
        });
        currentSession = result.id;
        document.getElementById('session_state').textContent = JSON.stringify(result, null, 2);
        await refreshSessionHistory();
      }

      async function openRegistrySession() {
        const statusFilterValue = document.getElementById('registry_open_status_filter').value || '';
        const statusFilter = statusFilterValue
          .split(',')
          .map((item) => item.trim())
          .filter((item) => item.length > 0);
        const pathInput = document.getElementById('registry_open_path').value;

        const payload = {
          slug: document.getElementById('registry_project_slug').value,
          path: pathInput || '/root/project-registry/exports/projects.json',
          status_filter: statusFilter,
        };

        const pickUrl = document.getElementById('registry_pick_url').value.trim();
        if (pickUrl) {
          payload.pick_url = pickUrl;
        }

        const result = await api('/api/sessions/from-registry', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        currentSession = result.id;
        document.getElementById('session_state').textContent = JSON.stringify(result, null, 2);
        document.getElementById('registry_open_result').textContent = JSON.stringify(
          {status: 'ok', session_id: result.id, project_id: result.session.project_id},
          null,
          2,
        );
        await refreshSessionHistory();
      }

      async function executeAction(payload, skipConfirm = false) {
        if (!currentSession) {
          return null;
        }

        const safePayload = {
          type: payload.type || document.getElementById('action_type').value,
          ref: safeString(payload.ref) || undefined,
          url: safeString(payload.url) || undefined,
          text: safeString(payload.text) || undefined,
          direction: safeString(payload.direction) || undefined,
          amount: payload.amount || 1,
          key: safeString(payload.key) || undefined,
          require_confirmation: !!payload.require_confirmation,
        };

        const stepwise = document.getElementById('stepwise_mode').checked;
        const forceConfirm = document.getElementById('require_confirmation').checked;
        const isRisky = ['click', 'type', 'navigate', 'scroll', 'press', 'back'].includes(safePayload.type);

        if (!skipConfirm && (stepwise || (forceConfirm && isRisky))) {
          const confirmation = confirm(
            `Action ${safePayload.type} ausführen?\n${JSON.stringify(safePayload, null, 2)}`,
          );
          if (!confirmation) {
            return {ok: false, error: 'Abgebrochen durch Nutzer', skipped: true};
          }
        }

        const result = await api(`/api/sessions/${currentSession}/action`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(safePayload),
        });
        document.getElementById('session_state').textContent = JSON.stringify(result, null, 2);
        await refreshSessionHistory();
        return result;
      }

      async function runAction() {
        const payload = {
          type: document.getElementById('action_type').value,
          ref: document.getElementById('action_ref').value || undefined,
          text: document.getElementById('action_text').value || undefined,
          url: document.getElementById('action_text').value || undefined,
          key: document.getElementById('action_text').value || undefined,
          amount: 1,
        };
        const result = await executeAction(payload);
        if (result?.ok === false && result?.skipped) {
          return;
        }
      }

      async function runVerify() {
        if (!currentSession) {
          return;
        }
        let checks = {};
        try {
          checks = JSON.parse(document.getElementById('verify_payload').value || '{}');
        } catch (_err) {
          checks = {};
        }
        if (!checks.must_contain_text) {
          checks.must_contain_text = [];
        }
        if (!checks.must_have_ref) {
          checks.must_have_ref = [];
        }
        const result = await api(`/api/sessions/${currentSession}/verify`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(checks),
        });
        document.getElementById('verify_result').textContent = JSON.stringify(result, null, 2);
      }

      async function runPresetAction() {
        const select = document.getElementById('action_preset_select');
        const preset = actionPresets.find((entry) => entry.id === select.value);
        if (!preset) {
          return;
        }
        await executeAction(preset.action, true);
      }

      async function refreshStats() {
        const stats = await api('/api/stats');
        document.getElementById('stat_projects').textContent = String(stats.projects_count || 0);
        document.getElementById('stat_sessions').textContent = String(stats.active_sessions || 0);
        document.getElementById('stat_runs').textContent = String(stats.run_count || 0);

        const details = {
          "Kanban": stats.kanban_count,
          "Kanban-Status": stats.kanban_status_counts,
          "Handoffs": stats.handoff_count,
          "Handoff-Status": stats.handoff_status_counts,
          "Session-Aktionen": stats.session_action_count,
          "Laufende Runs": stats.run_status_counts,
          "Storage": stats.storage_root,
        };
        document.getElementById('stats_detail').textContent = JSON.stringify(details, null, 2);
        document.getElementById('stats_updated').textContent = `Zuletzt aktualisiert: ${stats.generated_at}`;
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

      async function handoffKanban() {
        const taskId = document.getElementById('kb_handoff_id').value;
        if (!taskId) {
          return;
        }

        await api(`/api/kanban/${taskId}/handoff`, {
          method: 'POST',
        });
        await refreshKanban();
        await refreshHandoffs();
      }

      async function refreshStatsLoop() {
        if (!document.getElementById('auto_refresh').checked) {
          return;
        }
        await refreshStats();
      }

      function initializeTemplateAndPresets() {
        renderTemplateSelect();
        const savedTemplateId = localStorage.getItem('live_ui_active_template_v1');
        const startTemplate =
          TEMPLATE_CATALOG.find((template) => template.id === savedTemplateId)
          || TEMPLATE_CATALOG[0];
        activeTemplate = startTemplate;
        document.getElementById('template_select').value = startTemplate.id;
        applyTemplate(startTemplate.id);

        const savedPlugins = storeGetPlugins();
        if (Array.isArray(savedPlugins) && savedPlugins.length > 0) {
          actionPresets = [...BUILTIN_PRESETS, ...savedPlugins];
          const packedPresetText = JSON.stringify(savedPlugins, null, 2);
          if (packedPresetText) {
            document.getElementById('plugin_json').value = packedPresetText;
          }
        }
        renderActionPresets();

        const projectJson = localStorage.getItem('live_ui_last_project_json');
        if (projectJson) {
          document.getElementById('project_url').value = projectJson;
        }
      }

      function persistSessionUrlHint() {
        const sessionUrl = document.getElementById('session_url').value;
        localStorage.setItem('live_ui_last_project_json', sessionUrl || '');
      }

      initializeTemplateAndPresets();
      refreshProjects();
      refreshKanban();
      refreshHandoffs();
      refreshStats();
      setInterval(refreshStatsLoop, 5000);

      document.getElementById('session_url').addEventListener('change', persistSessionUrlHint);
      document.getElementById('auto_refresh').addEventListener('change', () => {
        if (document.getElementById('auto_refresh').checked) {
          refreshStats();
        }
      });
    </script>
  </body>
</html>
"""
