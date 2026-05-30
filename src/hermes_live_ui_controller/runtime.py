"""Runtime helpers shared by CLI and Web UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .adapter import InMemoryBrowserAdapter
from .controller import BrowserAdapter, NotImplementedAdapter
from .models import RunTask
from .playwright_adapter import PlaywrightBrowserAdapter


@dataclass
class AdapterConfig:
    """Runtime adapter selection for one run."""

    mode: str = "none"
    mock_pages: str | None = None
    no_headless: bool = False
    slow_mo_ms: int = 0


def build_mock_adapter(mock_pages: str | None, task_url: str) -> InMemoryBrowserAdapter:
    if not mock_pages:
        raise ValueError("--mock-pages is required when using in-memory mode")

    pages_path = Path(mock_pages)
    payload = json_load(pages_path)

    if "start_url" not in payload:
        payload["start_url"] = task_url

    return InMemoryBrowserAdapter(pages=payload, start_url=payload["start_url"])


def build_playwright_adapter(url: str, config: AdapterConfig) -> PlaywrightBrowserAdapter:
    return PlaywrightBrowserAdapter(
        start_url=url,
        headless=not config.no_headless,
        slow_mo_ms=config.slow_mo_ms,
    )


def build_adapter(task: RunTask, config: AdapterConfig) -> BrowserAdapter:
    if config.mode == "mock":
        return build_mock_adapter(mock_pages=config.mock_pages, task_url=task.url)

    if config.mode == "playwright":
        return build_playwright_adapter(task.url, config)

    if config.mode == "none":
        return NotImplementedAdapter()

    raise ValueError(f"unknown adapter '{config.mode}'")


def build_web_adapter(url: str, config: AdapterConfig) -> BrowserAdapter:
    if config.mode in {"mock", "none"}:
        raise ValueError("Web UI sessions require a Playwright adapter")

    if config.mode == "playwright":
        return build_playwright_adapter(url, config)

    raise ValueError(f"unknown adapter '{config.mode}'")


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
