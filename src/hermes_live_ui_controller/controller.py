"""Execution loop for live-web-ui tasks."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapter import BrowserAdapter
from .models import RunResult, RunTask, SnapshotState, Step, StepAction, StepResult, VerifyResult
from .verifier import UrlGuard, Verifier

logger = logging.getLogger(__name__)


@dataclass
class LiveWebUIRunner:
    """Run tasks in deterministic Action → Observe → Verify loop."""

    adapter: BrowserAdapter
    auto_confirm: bool = False
    output_dir: str | Path | None = None

    def run(self, task: RunTask) -> RunResult:
        run_id = task.effective_session_id()
        start = datetime.now(tz=UTC)

        steps_results: list[StepResult] = []
        failures = 0
        turns_left = task.policy.max_turns
        history: list[dict[str, Any]] = []

        verifier = Verifier(
            checks=task.checks,
            expected_delta=task.expected_delta,
            snapshot_stability_threshold=task.policy.snapshot_stability_threshold,
        )
        url_guard = UrlGuard(task.policy.allowed_domains)

        logger.info("Starting task", extra={"task_id": task.task_id, "session": run_id})

        baseline_snapshot = self._safe_snapshot()

        for index, step in enumerate(task.steps):
            if turns_left <= 0:
                steps_results.append(
                    StepResult(
                        index=index,
                        name=step.name,
                        action=step.action,
                        success=False,
                        attempts=0,
                        verify=VerifyResult(
                            passed=False,
                            failures=[],
                            debug={"reason": "max_turns reached"},
                        ),
                        snapshot_before=baseline_snapshot,
                        snapshot_after=self._safe_snapshot(),
                        error="max_turns reached before execution",
                    )
                )
                failures += 1
                break

            try:
                result = self._run_single_step(
                    index=index,
                    step=step,
                    run_task=task,
                    verifier=verifier,
                    url_guard=url_guard,
                    turns_left=turns_left,
                )
                turns_left -= result.attempts
            except RuntimeError as err:
                failure_msg = str(err)
                logger.error("Step execution failed", extra={"step": step.name, "error": failure_msg})
                result = StepResult(
                    index=index,
                    name=step.name,
                    action=step.action,
                    success=False,
                    attempts=0,
                    verify=VerifyResult(
                        passed=False,
                        failures=[],
                        debug={"reason": failure_msg},
                    ),
                    snapshot_before=baseline_snapshot,
                    snapshot_after=self._safe_snapshot(),
                    error=failure_msg,
                )

            steps_results.append(result)
            if result.snapshot_after is not None:
                baseline_snapshot = result.snapshot_after

            history.append(self._dump_step_result(result))

            if not result.success:
                failures += 1
                if task.fallback.rollback:
                    self._rollback()
                if failures > task.fallback.max_failures:
                    break

            if turns_left <= 0:
                break

        end = datetime.now(tz=UTC)
        success = (
            len(steps_results) > 0
            and all(item.success for item in steps_results)
            and failures <= task.fallback.max_failures
        )

        run_result = RunResult(
            task_id=task.task_id,
            session_id=run_id,
            success=success,
            steps=steps_results,
            failures=failures,
            start_iso=start,
            end_iso=end,
            reason=None if success else "Step failed or turn budget exhausted",
        )

        self._persist_run(run_id, run_result, task, history)
        return run_result

    def _run_single_step(
        self,
        index: int,
        step: Step,
        run_task: RunTask,
        verifier: Verifier,
        url_guard: UrlGuard,
        turns_left: int,
    ) -> StepResult:
        if turns_left <= 0:
            raise RuntimeError("max_turns exceeded")

        if self._is_blocked(step.action, run_task.policy.blocked_actions):
            raise RuntimeError(f"Action '{step.action.type}' blocked by policy")

        if step.pre_checks:
            pre_check_result = verifier.verify(
                snapshot_before=None,
                snapshot_after=self._safe_snapshot(),
                override_checks=step.pre_checks,
            )
            if not pre_check_result.passed:
                failure = pre_check_result.failures[0]
                raise RuntimeError(f"Pre-checks failed: {failure.scope}: {failure.message}")

        plan = [step.action]
        if step.max_retries > 0:
            plan.extend([step.action] * step.max_retries)
        if step.alternative_action:
            plan.append(step.alternative_action)

        snapshot_before = self._safe_snapshot()
        last_verify = VerifyResult.ok()
        last_error: str | None = None
        used_attempts = 0

        for action in plan:
            if used_attempts >= turns_left:
                break

            used_attempts += 1
            try:
                self._ensure_allowed_domain(action, url_guard)
                self._require_confirmation(action, step, run_task)
                self._execute_action(action)

                # Observe loop: snapshot + console + verify.
                post_snapshot = self._safe_snapshot()
                events = self.adapter.console(clear=True, expression=None)
                post_snapshot.console_errors = sum("error" in item.lower() for item in events)

                checks = step.post_checks if step.post_checks else None
                last_verify = verifier.verify(
                    snapshot_before=snapshot_before,
                    snapshot_after=post_snapshot,
                    override_checks=checks,
                    apply_stability=(action.type not in {"navigate", "click", "back"}),
                )
                if last_verify.passed:
                    return StepResult(
                        index=index,
                        name=step.name,
                        action=action,
                        success=True,
                        attempts=used_attempts,
                        verify=last_verify,
                        snapshot_before=snapshot_before,
                        snapshot_after=post_snapshot,
                        error=None,
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning(
                    "Step attempt failed",
                    extra={"step": step.name, "attempt": used_attempts, "error": last_error},
                )

        # If here, all retries failed.
        return StepResult(
            index=index,
            name=step.name,
            action=step.action,
            success=False,
            attempts=used_attempts,
            verify=last_verify,
            snapshot_before=snapshot_before,
            snapshot_after=self._safe_snapshot(),
            error=last_error or _first_failure_msg(last_verify) or "Step failed",
        )

    def _ensure_allowed_domain(self, action: StepAction, url_guard: UrlGuard) -> None:
        if action.type == "navigate" and action.url and not url_guard.check(action.url):
            raise RuntimeError("navigation blocked by allowed_domains")

    def _require_confirmation(self, action: StepAction, step: Step, task: RunTask) -> None:
        policy_request = action.type in task.policy.require_confirm_actions
        should_confirm = (
            step.require_confirmation is True
            or action.require_confirmation
            or (step.require_confirmation is None and policy_request)
        )

        if should_confirm and not self.auto_confirm:
            raise RuntimeError("confirmation required but auto_confirm is disabled")

    def _is_blocked(self, action: StepAction, blocked: list[str]) -> bool:
        candidate_tokens = {
            (action.type or "").lower(),
            (action.ref or "").lower(),
            (action.url or "").lower(),
            (action.key or "").lower(),
        }

        for blocked_action in blocked:
            token = blocked_action.strip().lower()
            if not token:
                continue
            if token in candidate_tokens:
                return True
            if any(token in value for value in candidate_tokens if value):
                return True
        return False

    def _execute_action(self, action: StepAction) -> None:
        if action.type == "navigate":
            self.adapter.navigate(action.url or "")
        elif action.type == "click":
            self.adapter.click(action.ref or "")
        elif action.type == "type":
            self.adapter.type(action.ref or "", action.text or "")
        elif action.type == "scroll":
            direction = action.direction.value if action.direction else "down"
            self.adapter.scroll(direction, action.amount or 1)
        elif action.type == "press":
            self.adapter.press(action.key or "")
        elif action.type == "back":
            self.adapter.back()
        elif action.type == "snapshot":
            self._safe_snapshot()
        else:
            raise RuntimeError(f"Unknown action type: {action.type}")

    def _safe_snapshot(self) -> SnapshotState:
        return self.adapter.snapshot(include_full=True)

    def _rollback(self) -> None:
        rollback = getattr(self.adapter, "rollback", None)
        if callable(rollback):
            success = rollback()
            logger.info("rollback executed", extra={"success": success})

    def _dump_step_result(self, step_result: StepResult) -> dict[str, Any]:
        return {
            "index": step_result.index,
            "name": step_result.name,
            "success": step_result.success,
            "attempts": step_result.attempts,
            "error": step_result.error,
            "action": step_result.action.model_dump(mode="json"),
            "verify": step_result.verify.model_dump(mode="json"),
        }

    def _persist_run(
        self,
        run_id: str,
        run_result: RunResult,
        task: RunTask,
        history: list[dict[str, Any]],
    ) -> None:
        if self.output_dir is None:
            return

        output = Path(self.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        run_path = output / f"{run_id}.json"
        data = {
            "run_id": run_id,
            "task": task.model_dump(mode="json"),
            "result": run_result.model_dump(mode="json"),
            "history": history,
        }
        run_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


class NotImplementedAdapter:
    """Default adapter used when no live browser backend is wired in."""

    def snapshot(self, include_full: bool = False) -> SnapshotState:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def navigate(self, url: str) -> None:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def click(self, ref: str) -> None:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def type(self, ref: str, text: str) -> None:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def scroll(self, direction: str, amount: int = 1) -> None:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def press(self, key: str) -> None:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")

    def back(self) -> None:
        raise RuntimeError("No browser backend configured")

    def rollback(self) -> bool:
        return False

    def console(self, clear: bool = False, expression: str | None = None) -> Sequence[str]:  # noqa: ARG002
        raise RuntimeError("No browser backend configured")


def _first_failure_msg(verify_result: VerifyResult) -> str:
    if not verify_result.failures:
        return ""
    first = verify_result.failures[0]
    return f"{first.scope}: {first.message}"
