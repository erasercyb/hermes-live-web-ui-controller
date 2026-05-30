"""Core task and policy models for the live web UI controller."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

ActionType = Literal["navigate", "click", "type", "scroll", "press", "back", "snapshot"]


class ScrollDirection(StrEnum):
    up = "up"
    down = "down"


class PolicyConfig(BaseModel):
    """Safety policy that governs one execution task."""

    allowed_domains: list[str] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)
    require_confirm_actions: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=35, gt=0)
    snapshot_stability_threshold: float = Field(default=0.85, gt=0.0, le=1.0)

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        return [value.lower().strip() for value in values if value.strip()]


class CheckConfig(BaseModel):
    """Verifier checks that must be evaluated after each action step."""

    must_have_ref: list[str] = Field(default_factory=list)
    must_contain_text: list[str] = Field(default_factory=list)
    console_no_fatal: bool = True
    max_console_errors: int = Field(default=0, ge=0)


class ExpectedDelta(BaseModel):
    target_selector_text: str | None = None
    snapshot_keyword_after: str | None = None


class FallbackConfig(BaseModel):
    max_failures: int = Field(default=2, ge=0)
    rollback: bool = False


class StepAction(BaseModel):
    """Single browser action representation."""

    type: ActionType
    url: str | None = None
    ref: str | None = None
    text: str | None = None
    direction: ScrollDirection | None = None
    amount: int | None = Field(default=1, ge=0)
    key: str | None = None
    require_confirmation: bool = False

    @field_validator("url")
    @classmethod
    def validate_navigation_url(cls, value: str | None) -> str | None:
        if value is None:
            return None

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Navigation URL must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("Navigation URL must contain a valid host")
        return value


class Step(BaseModel):
    """A unit of work in a run manifest."""

    name: str
    action: StepAction
    alternative_action: StepAction | None = None
    post_checks: CheckConfig | None = None
    pre_checks: CheckConfig | None = None
    require_confirmation: bool | None = None
    max_retries: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class RunTask(BaseModel):
    """Top-level task manifest schema."""

    task_id: str
    goal: str
    url: str
    policy: PolicyConfig = PolicyConfig()
    checks: CheckConfig = CheckConfig()
    expected_delta: ExpectedDelta = ExpectedDelta()
    fallback: FallbackConfig = FallbackConfig()
    steps: list[Step]
    session_id: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("task_id must not be empty")
        return normalized

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal must not be empty")
        return normalized

    def effective_session_id(self) -> str:
        if self.session_id:
            return self.session_id
        return f"{self.task_id}-{uuid4().hex[:10]}"

    @classmethod
    def from_path(cls, path: str | Path) -> RunTask:
        import json

        task_path = Path(path)
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        return cls.model_validate(payload)


class SnapshotState(BaseModel):
    """Normalized snapshot snapshot from the browser adapter."""

    ref_ids: list[str] = Field(default_factory=list)
    text: str = ""
    title: str = ""
    url: str = ""
    console_errors: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def normalized_text(self) -> str:
        return self.text.lower()


class VerifyFailure(BaseModel):
    scope: str
    message: str


class VerifyResult(BaseModel):
    passed: bool
    failures: list[VerifyFailure] = Field(default_factory=list)
    debug: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def ok(cls) -> VerifyResult:
        return cls(passed=True)

    @classmethod
    def fail(cls, message: str, scope: str = "global") -> VerifyResult:
        return cls(passed=False, failures=[VerifyFailure(scope=scope, message=message)])


class StepResult(BaseModel):
    index: int
    name: str
    action: StepAction
    success: bool
    attempts: int
    verify: VerifyResult
    snapshot_before: SnapshotState | None = None
    snapshot_after: SnapshotState | None = None
    error: str | None = None


class RunResult(BaseModel):
    task_id: str
    session_id: str
    success: bool
    steps: list[StepResult]
    failures: int
    start_iso: datetime
    end_iso: datetime
    reason: str | None = None
