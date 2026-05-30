# Changelog

## 0.2.0 - 2026-05-30

### Added
- Optionales Playwright-Backend als zweiter Adapter (`src/hermes_live_ui_controller/playwright_adapter.py`).
- CLI-Erweiterung: `--adapter playwright`, `--no-headless`, `--slow-mo-ms`.
- PR-spezifischer GitHub-Workflow (`PR Checks`) mit Paket-Build-Check und PR-Zusammenfassungskommentar.
- `CONTRIBUTING.md`, `PULL_REQUEST_TEMPLATE.md`, `CODEOWNERS`, Release-/PR-Ecosystem-Files vorbereitet.

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
