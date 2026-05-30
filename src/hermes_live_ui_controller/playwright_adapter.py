"""Playwright adapter for live browser execution.

This adapter is optional and is only imported when explicitly selected.
It intentionally keeps dependencies soft-failed so environments without
Playwright can still use in-memory mode and offline tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SnapshotState


class PlaywrightDependencyError(RuntimeError):
    """Raised when Playwright is required but not installed."""


@dataclass
class PlaywrightBrowserAdapter:
    """Adapter implementation backed by a real Playwright page."""

    start_url: str
    headless: bool = True
    slow_mo_ms: int = 0

    _playwright: object | None = None
    _browser: object | None = None
    _context: object | None = None
    _page: object | None = None
    _console_events: list[str] = None

    def __post_init__(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except Exception as exc:
            raise PlaywrightDependencyError(
                "Playwright dependency not installed. Install with: pip install hermes-live-web-ui-controller[browser]"
            ) from exc

        self._console_events = []
        self._playwright = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        if self.slow_mo_ms:
            launch_kwargs["slow_mo"] = self.slow_mo_ms

        try:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
            self._page.on("console", self._on_console)
            self._page.goto(self.start_url)
        except Exception:
            self.close()
            raise PlaywrightDependencyError(
                "Playwright browser binary not available. Run: python -m playwright install"
            ) from None

    @staticmethod
    def _extract_refs(dom_snapshot: str) -> list[str]:
        # Keep this intentionally conservative: id and data-ref.
        import re

        result: list[str] = []
        # data-ref="..."
        for match in re.finditer(r"data-ref=\"([^\"]+)\"", dom_snapshot):
            value = match.group(1).strip()
            if value and value not in result:
                result.append(value)
        # fallback id attributes
        for match in re.finditer(r"\bid=\"([^\"]+)\"", dom_snapshot):
            value = match.group(1).strip()
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _normalize_text(content: str | None) -> str:
        return (content or "").strip()

    def _on_console(self, message) -> None:
        try:
            self._console_events.append(str(message.text))
        except Exception:
            self._console_events.append("<console-message-unreadable>")

    def snapshot(self, include_full: bool = False) -> SnapshotState:  # noqa: ARG002
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")

        content = self._normalize_text(self._page.inner_text("body"))
        html = str(self._page.content())
        refs = self._extract_refs(html)

        title = ""
        try:
            title = self._page.title() or ""
        except Exception:
            title = ""

        return SnapshotState(
            ref_ids=refs,
            text=content,
            title=title,
            url=str(self._page.url),
            console_errors=0,
            meta={
                "include_full": include_full,
                "history": [],
            },
        )

    def navigate(self, url: str) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        self._page.goto(url)

    def click(self, ref: str) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        selector = f"[data-ref='{ref}'], #{ref}"
        self._page.click(selector)

    def type(self, ref: str, text: str) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        selector = f"[data-ref='{ref}'], #{ref}"
        self._page.fill(selector, text)
        self._console_events.append(f"type:{ref}")

    def scroll(self, direction: str, amount: int = 1) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        delta_y = 0
        delta_x = 0
        if direction == "up":
            delta_y = -abs(int(amount)) * 120
        else:
            delta_y = abs(int(amount)) * 120
        self._page.mouse.wheel(delta_x=delta_x, delta_y=delta_y)
        self._console_events.append("scroll")

    def press(self, key: str) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        self._page.keyboard.press(key)
        self._console_events.append(f"press:{key}")

    def back(self) -> None:
        if self._page is None:
            raise PlaywrightDependencyError("Playwright page is not available")
        self._page.go_back()

    def console(self, clear: bool = False, expression: str | None = None) -> list[str]:
        events = list(self._console_events)
        if clear:
            self._console_events = []

        if expression:
            return [entry for entry in events if expression in entry]
        return events

    def rollback(self) -> bool:
        if self._page is None:
            return False
        # pragmatic no-op that keeps contract stable
        return True

    def close(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
