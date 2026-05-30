from argparse import Namespace
from types import SimpleNamespace

import pytest

from hermes_live_ui_controller import cli
from hermes_live_ui_controller.models import RunTask


def test_cli_run_from_manifest_success(monkeypatch, capsys) -> None:
    def fake_runner(*_args, **_kwargs):
        class _Runner:
            def run(self, task):
                del task
                return SimpleNamespace(
                    success=True,
                    failures=0,
                    task_id="edit-hero-section-2026-05-24",
                    session_id="session-id",
                    steps=[SimpleNamespace(success=True, error=None)],
                )

        return _Runner()

    monkeypatch.setattr(cli, "LiveWebUIRunner", fake_runner)

    args = Namespace(
        manifest="examples/task_edit_hero.json",
        adapter="none",
        mock_pages=None,
        output_dir=None,
        auto_confirm=False,
        no_headless=False,
        slow_mo_ms=0,
    )

    exit_code = cli.run_from_manifest(args)

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "OK: edit-hero-section-2026-05-24" in captured
    assert "steps=1 failures=0" in captured


def test_cli_run_from_manifest_failure(monkeypatch, capsys) -> None:
    def fake_runner(*_args, **_kwargs):
        class _Runner:
            def run(self, task):
                del task
                return SimpleNamespace(
                    success=False,
                    failures=1,
                    task_id="task-id",
                    session_id="session-id",
                    steps=[
                        SimpleNamespace(success=False, error="blocked", name="Step A"),
                        SimpleNamespace(success=True, error=None, name="Step B"),
                    ],
                )

        return _Runner()

    monkeypatch.setattr(cli, "LiveWebUIRunner", fake_runner)

    args = Namespace(
        manifest="examples/task_edit_hero.json",
        adapter="none",
        mock_pages=None,
        output_dir=None,
        auto_confirm=False,
        no_headless=False,
        slow_mo_ms=0,
    )

    exit_code = cli.run_from_manifest(args)

    captured = capsys.readouterr().out
    assert exit_code == 2
    assert "FAIL: task-id" in captured
    assert "- Step A: blocked" in captured


def test_build_playwright_adapter_raises_when_dependency_missing(monkeypatch) -> None:
    task = RunTask.from_path("examples/task_edit_hero.json")

    def raise_dependency_error(*_args, **_kwargs):
        raise cli.PlaywrightDependencyError("missing playwright")

    monkeypatch.setattr(cli, "PlaywrightBrowserAdapter", raise_dependency_error)

    with pytest.raises(SystemExit) as excinfo:
        cli._build_playwright_adapter(task, headless=True, slow_mo_ms=0)
    assert "missing playwright" in str(excinfo.value)
