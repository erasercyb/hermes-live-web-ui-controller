# Contributing Guidelines

## Projektkontext

Dieses Projekt implementiert einen kontrollierten **Action → Observe → Verify**-Loop für Web-UI-Automatisierung.
Bitte halte dich an die vorhandene Architektur und nutze den Manifest-basierten Workflow.

## Branches und Commits

- Nutze kurze, prägnante Branch-Namen:
  - `feat/...`
  - `fix/...`
  - `docs/...`
  - `refactor/...`
  - `ci/...`
- Commit Messages nach **Conventional Commits**:
  - `feat: ...`
  - `fix: ...`
  - `chore: ...`

## Entwicklungs-Setup

```bash
python -m pip install -e ".[dev]"
```

Optional für echte Browser-Tests:

```bash
python -m pip install -e ".[browser]"
python -m playwright install
```

## Qualitätsstandards

Vor jedem Push/PR bitte lokal mindestens ausführen:

```bash
ruff format --check src tests
ruff check src tests
pytest
python -m build
```

## PR-Review-Standard

Beim Erstellen von PRs bitte:
- Architekturbezug klar beschreiben (welcher Teil des Blueprints wurde umgesetzt)
- Risiken/Abkürzungen explizit auflisten
- Änderungstests dokumentieren
- ggf. Demo-Ausführung (Mock/Playwright) erwähnen
