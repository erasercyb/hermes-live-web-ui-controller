# Hermes Live-Web-UI Controller

Dieses Repository implementiert den Plan **„Hermes Live‑Web‑UI Editing (nicht nur Prompt‑Loop)“** als lauffähige Python-Bibliothek + CLI.

## Ziele

- Automatisierter, iterativer **Action → Observe → Verify → Adjust** Loop für Web-UI-Aufgaben
- Sicherheitsgates für Domänen, geblockte Aktionen, Bestätigungen und Max-Iterationen
- Konfigurierbare Tasks als JSON-Manifest (Schema, Policy, Checks, erwartete Deltas)
- Strukturierte Ausführung mit Logging, Re-Verification, Retry/Recovery und Rollback-Hooks
- Standards: Tests, Lint, CI, review-ready Struktur

## Architektur

- `models.py` – Domänenmodelle (Policy, Checks, Task, Schritte)
- `adapter.py` – Browser-Adapter-Interface + In-Memory-Adapter (für Tests) + Stub für zukünftige Hermes-/Playwright-Adapter
- `verifier.py` – deterministische Verifier-Regeln (Ref-Checks, Text-Checks, Konsolenfehler, Toleranz)
- `controller.py` – Kernloop: für jeden Schritt `action` -> `snapshot` -> `console` -> `verify`
- `cli.py` – ausführbare CLI zum Starten eines Task-Manifests
- `schemas/run_task_schema.json` – JSON-Schema für Task-Inputs

## Unterstützte Aktionen

- `navigate` (`url` erforderlich)
- `click` (`ref` erforderlich)
- `type` (`ref`, `text`)
- `scroll` (`direction`, `amount` optional)
- `press` (`key`)
- `back`

Jede Aktion wird nach Ausführung durch den Verifier geprüft.

## Task-Flow auf einen Blick

```text
Task lädt Policy + Checks
  -> Initialer Snapshot (optional)
  -> Für jeden Schritt:
     - Optionaler Vor-Verify (z. B. Kontextsicherung)
     - Aktion ausführen
     - Snapshot + Console einholen
     - Verifier anwenden
     - Bei Fehler: Retry (falls konfiguriert), optionaler Rollback, sonst sauber abbrechen
  -> Ergebnisreport + Artefakte (Log, Checkliste, Verlauf)
```

## Installation

```bash
python -m pip install -e .
```

Entwicklungsabhängigkeiten:

```bash
python -m pip install -e " .[dev]"
```

Für echte Browser-Runs (Playwright):

```bash
python -m pip install -e ".[browser]"
python -m playwright install
```

## Schnellstart

```bash
python -m hermes_live_ui_controller \
  --manifest examples/task_edit_hero.json \
  --adapter mock \
  --mock-pages examples/pages_mock.json \
  --output-dir ./.runs
```

## Beispiel-Manifest

```json
{
  "task_id": "edit-hero-section-2026-05-24",
  "goal": "Passe Hero Titel und CTA an",
  "url": "https://deine-webui.internal/",
  "policy": {
    "allowed_domains": ["deine-webui.internal"],
    "blocked_actions": ["delete_user", "payment", "export_raw_db"],
    "require_confirm_actions": ["publish", "delete", "submit"],
    "max_turns": 35,
    "snapshot_stability_threshold": 0.85
  },
  "checks": {
    "must_have_ref": ["e12", "e13"],
    "must_contain_text": ["Hero gespeichert", "CTA aktiv"],
    "console_no_fatal": true,
    "max_console_errors": 0
  },
  "expected_delta": {
    "target_selector_text": "Willkommen zurück",
    "snapshot_keyword_after": "Hero gespeichert"
  },
  "fallback": {
    "max_failures": 2,
    "rollback": true
  },
  "steps": [
    {
      "name": "dashboard öffnen",
      "action": {"type": "navigate", "url": "https://deine-webui.internal/"},
      "post_checks": {
        "must_contain_text": ["Dashboard"]
      }
    },
    {
      "name": "login sichern",
      "action": {"type": "click", "ref": "e2"},
      "alternative_action": {"type": "press", "key": "Enter"}
    },
    {
      "name": "hero-überschrift editieren",
      "action": {"type": "type", "ref": "e12", "text": "Willkommen zurück"}
    },
    {
      "name": "cta speichern",
      "action": {"type": "click", "ref": "e13"},
      "require_confirmation": false
    }
  ]
}
```

## Tests & Qualität

- `ruff` für Lint & Format (CI)
- `pytest` mit gezielten Unit-Tests für Model-Validierung, Controller-Verhalten und Verifier

```bash
python -m pytest
ruff check src tests
ruff format --check src tests
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) prüft:

- `ruff check`
- `ruff format --check`
- `pytest`

## Nächste Entwicklungsstufen

- Playwright-Adapter als echte Ausführungsebene
- Dashboard-Metriken (Erfolgsquote, Schritte bis Ziel, Retry-Rate)
- Persistente Run-Datenbank + Verlauf
- Team-/Vorlagenverzeichnis für häufige Web-UI-Patterns
