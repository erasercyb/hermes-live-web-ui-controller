"""Browser adapter abstraction used by the controller."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .models import SnapshotState


class BrowserAdapter(Protocol):
    """Adapter interface for the loop runner.

    A production implementation can wrap Hermes browser tools, Playwright, etc.
    """

    def snapshot(self, include_full: bool = False) -> SnapshotState: ...

    def navigate(self, url: str) -> None: ...

    def click(self, ref: str) -> None: ...

    def type(self, ref: str, text: str) -> None: ...

    def scroll(self, direction: str, amount: int = 1) -> None: ...

    def press(self, key: str) -> None: ...

    def back(self) -> None: ...

    def console(self, clear: bool = False, expression: str | None = None) -> Sequence[str]: ...

    def rollback(self) -> bool:
        """Optional capability.

        Returns ``True`` if rollback succeeded, otherwise False.
        """

        return False


@dataclass
class InMemoryBrowserAdapter:
    """Test adapter with simple in-memory pages.

    This adapter is deterministic and meant for unit tests and dry-runs.
    """

    pages: dict[str, dict[str, Any]]
    start_url: str
    current_url: str | None = None
    console_events: list[str] = None
    current_text: str = ""
    ref_ids: list[str] = None
    history: list[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "console_events", [])
        object.__setattr__(self, "history", [])
        object.__setattr__(self, "ref_ids", [])
        if self.current_url is None:
            self.current_url = self.start_url
        self._apply_page(self.current_url)

    def _apply_page(self, url: str) -> None:
        page = self.pages.get(url, {"text": "", "refs": []})
        self.current_text = page.get("text", "")
        self.ref_ids = list(page.get("refs", []))

    def snapshot(self, include_full: bool = False) -> SnapshotState:
        del include_full
        return SnapshotState(
            ref_ids=self.ref_ids,
            text=self.current_text,
            title=self.pages.get(self.current_url, {}).get("title", ""),
            url=self.current_url or "",
            console_errors=sum("error" in item.lower() for item in self.console_events),
            meta={"history_depth": len(self.history)},
        )

    def navigate(self, url: str) -> None:
        if url not in self.pages:
            raise RuntimeError(f"Unknown URL in in-memory adapter: {url}")
        if self.current_url:
            self.history.append(self.current_url)
        self.current_url = url
        self._apply_page(url)

    def click(self, ref: str) -> None:
        if ref not in self.ref_ids:
            raise RuntimeError(f"ref '{ref}' not present in current snapshot")
        transitions = self.pages.get(self.current_url, {}).get("transitions", {})
        target = transitions.get(ref)
        if target is None:
            # idempotent safe failure path for click without side effects
            return
        if isinstance(target, str):
            self.navigate(target)
            return
        if isinstance(target, dict):
            new_text = target.get("text")
            if new_text is not None:
                self.current_text = new_text
                return
            raise RuntimeError(f"Unsupported transition payload for ref '{ref}'")

    def type(self, ref: str, text: str) -> None:
        if ref not in self.ref_ids:
            raise RuntimeError(f"ref '{ref}' not present in current snapshot")
        editable = self.pages.get(self.current_url, {}).get("editable_refs", {})
        if ref not in editable:
            raise RuntimeError(f"ref '{ref}' is not editable on current page")
        self.current_text = text
        self.console_events.append(f"type: {ref}")

    def scroll(self, direction: str, amount: int = 1) -> None:
        del direction, amount
        self.console_events.append("scroll")

    def press(self, key: str) -> None:
        self.console_events.append(f"press:{key}")

    def back(self) -> None:
        if not self.history:
            return
        self.current_url = self.history.pop()
        self._apply_page(self.current_url)

    def console(self, clear: bool = False, expression: str | None = None) -> list[str]:
        del expression
        events = list(self.console_events)
        if clear:
            self.console_events = []
        return events
