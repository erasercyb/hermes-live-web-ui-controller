"""Verifier logic: evaluate whether a snapshot and console satisfy checks."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .models import CheckConfig, ExpectedDelta, SnapshotState, VerifyFailure, VerifyResult


@dataclass
class Verifier:
    checks: CheckConfig
    expected_delta: ExpectedDelta
    snapshot_stability_threshold: float

    def verify(
        self,
        snapshot_before: SnapshotState | None,
        snapshot_after: SnapshotState,
        override_checks: CheckConfig | None = None,
        *,
        apply_stability: bool = True,
    ) -> VerifyResult:
        check = override_checks or self.checks
        failures: list[tuple[str, str]] = []

        for ref in check.must_have_ref:
            if ref not in snapshot_after.ref_ids:
                failures.append(("refs", f"Expected ref {ref} not present in snapshot"))

        snapshot_text = snapshot_after.normalized_text
        for needle in check.must_contain_text:
            if needle.lower() not in snapshot_text:
                failures.append(("text", f"Expected text '{needle}' not found"))

        if check.console_no_fatal and snapshot_after.console_errors > check.max_console_errors:
            failures.append(("console", "Too many console errors"))

        if self.expected_delta.target_selector_text:
            text = snapshot_after.normalized_text
            if self.expected_delta.target_selector_text.lower() not in text:
                failures.append(
                    (
                        "delta",
                        f"Expected target text '{self.expected_delta.target_selector_text}' not observed in snapshot",
                    )
                )

        if self.expected_delta.snapshot_keyword_after:
            if self.expected_delta.snapshot_keyword_after.lower() not in snapshot_after.normalized_text:
                failures.append(
                    (
                        "delta",
                        f"Expected keyword '{self.expected_delta.snapshot_keyword_after}' "
                        "not observed in post-action snapshot",
                    )
                )

        if snapshot_before is not None and apply_stability:
            self._check_snapshot_stability(snapshot_before, snapshot_after, failures)

        if failures:
            return VerifyResult(
                passed=False,
                failures=[VerifyFailure(scope=scope, message=message) for scope, message in failures],
                debug={
                    "delta_text_len": max(0, len(snapshot_after.text) - len(snapshot_before.text))
                    if snapshot_before
                    else len(snapshot_after.text),
                },
            )

        return VerifyResult.ok()

    def _check_snapshot_stability(
        self,
        before: SnapshotState,
        after: SnapshotState,
        failures: list[tuple[str, str]],
    ) -> None:
        if not before.text or not after.text:
            return

        before_words = before.normalized_text.split()
        after_words = after.normalized_text.split()
        if len(before_words) < 3 or len(after_words) < 3:
            return
        if not before_words or not after_words:
            return

        before_set = set(before_words)
        intersection = before_set.intersection(after_words)
        ratio = len(intersection) / max(1, len(before_set | set(after_words)))

        if ratio > 1:
            ratio = 1.0

        if ratio < self.snapshot_stability_threshold:
            failures.append(
                (
                    "stability",
                    f"Snapshot stability ratio {ratio:.2f} below threshold {self.snapshot_stability_threshold}",
                )
            )


@dataclass
class UrlGuard:
    allowed_domains: list[str]

    def check(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        host = host.lower()

        if not self.allowed_domains:
            return True

        return any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)
