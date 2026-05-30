from hermes_live_ui_controller.models import CheckConfig, ExpectedDelta, SnapshotState
from hermes_live_ui_controller.verifier import Verifier


def test_verifier_passes_with_matching_refs_text() -> None:
    before = SnapshotState(ref_ids=["x", "y"], text="Dashboard")
    after = SnapshotState(ref_ids=["x", "y", "z"], text="Hero gespeichert\ncta aktiv")

    verifier = Verifier(
        checks=CheckConfig(must_have_ref=["z"], must_contain_text=["Hero gespeichert"]),
        expected_delta=ExpectedDelta(snapshot_keyword_after="cta aktiv"),
        snapshot_stability_threshold=0.1,
    )

    result = verifier.verify(before, after)
    assert result.passed is True
    assert result.failures == []


def test_verifier_fails_on_missing_ref_and_error() -> None:
    before = SnapshotState(ref_ids=["a", "b"], text="old text")
    after = SnapshotState(ref_ids=["a", "x"], text="some new state", console_errors=3)

    verifier = Verifier(
        checks=CheckConfig(must_have_ref=["missing"], must_contain_text=["hero"], max_console_errors=0),
        expected_delta=ExpectedDelta(target_selector_text="Hallo"),
        snapshot_stability_threshold=0.9,
    )

    result = verifier.verify(before, after)
    assert result.passed is False
    assert any(f.scope == "refs" for f in result.failures)
    assert any(f.scope == "text" for f in result.failures)
    assert any(f.scope == "console" for f in result.failures)
