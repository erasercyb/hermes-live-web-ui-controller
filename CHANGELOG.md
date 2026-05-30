# Changelog

## Unreleased

### Added
- Web-UI-Server ergänzt (`hermes-live-web-ui-server`) für direktes Laden von Projekt-Web-UIs,
  Live-Interaktionen, Run-Dispatching und Kanban-Board mit Subagent-Handoff-Markierung.
- `src/hermes_live_ui_controller/web_server.py` mit persistenter Projekt-/Run-/Kanban-Fassung.
- Kanban-Subagent-Handoff-Route hinzugefügt (`POST /api/kanban/{id}/handoff`) zur Markierung komplexer Aufgaben.
- `runtime.py` mit getrennter Adapter-Builder-Logik zur Wiederverwendung von CLI + Web-Server.

- Coverage-Gate (`pytest --cov ... --cov-fail-under=80`) für Pull-Requests.
- Node-24-Forwarding für GitHub Actions via `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`.
- Zusätzliche CLI-Unit-Tests (`tests/test_cli.py`) für Adapter/Run-Flow.

### Changed
- `pyproject.toml`-Abhängigkeiten um `pytest-cov`/`jsonschema` (Dev-Extras) erweitert.
- CI/PR-Workflows inkl. Release ergänzt um Konsistenz-Checks.

## 0.2.0 - 2026-05-30

### Added
- Optionales Playwright-Backend als zweiter Adapter (`src/hermes_live_ui_controller/playwright_adapter.py`).
- CLI-Erweiterung: `--adapter playwright`, `--no-headless`, `--slow-mo-ms`.
- Neue PR-/Release-Workflows, Beitrags- und Review-Doku sowie Code-Owner-Einträge.
- Erster publizierter Release-Tag `v0.2.0` mit GitHub-Release.

### Changed
- `ci.yml` läuft weiterhin für `main`-Pushes.
- `README.md` um Browser-Extra und Mock-Schnellstart angepasst.
- `adapter`-Kontrakt um `rollback()`-Kompatibilität für Mock-Adapter ergänzt.

### Fixed
- Konsistente Adapter-Typ-Verträglichkeit
- CLI-Teardown-Sicherheit (Adapter Close im `finally`-Block)

## 0.1.0 - 2026-05-30

- Initiale Implementierung des Hermes Live-Web-UI Controllers:
  - Manifest-Modelle, Adapter-Interface, Verifier, Controller, CLI, Tests, CI.
