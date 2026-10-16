"""A routine cooldown releases the slot without hiding failures or daily gaps."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from radar_backend.application.growth_daily import collect_daily_observations
from radar_backend.domain.growth import growth_collection_complete
from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.publication.growth import (
    growth_checkpoint_payload, growth_publication_payload, stamp_growth_publication,
)
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache
from scripts import classify_growth_attempt as classification


NOW = datetime(2026, 10, 16, 5, 15, tzinfo=timezone.utc)


def reports():
    rows = [{"appid": aid, "followers": 6000, "release_start": "2026-10-16",
             "group_id64": str(GROUP_BASE + aid)} for aid in (1, 2)]
    checkpoint = {"official_results": {
        str(aid): {"appid": aid, "group_id64": str(GROUP_BASE + aid),
                   "official_followers": 6000,
                   "official_checked_at_taipei": (NOW if aid == 1 else NOW - timedelta(days=1)).isoformat()}
        for aid in (1, 2)}, "next_request_after_taipei": (NOW + timedelta(minutes=45)).isoformat()}
    def forbidden(*args):
        raise AssertionError("An acknowledged cooldown must neither sleep nor call Steam")
    original = collect_daily_observations(
        rows, {}, checkpoint, client=SimpleNamespace(fetch=forbidden),
        cache=OfficialFollowerCache(checkpoint), cooldown=CooldownStore(checkpoint),
        clock=lambda: NOW, monotonic=lambda: 0, sleep=forbidden,
        persist=lambda *_: None, max_cooldown_wait=0, state_saved=True,
    )
    # Use the real checkpoint and public reconciliation followed by receipt
    # stamping, rather than crafting the desired final shape in the test.
    delivered = growth_checkpoint_payload(original, checkpoint, now=NOW)
    delivered = growth_publication_payload(delivered, {"count": 2, "games": rows}, NOW)
    delivered = stamp_growth_publication(delivered, NOW, state_persisted=True, published=True)
    return original, delivered


def test_actual_cooldown_is_deferred_after_real_delivery_without_daily_success():
    original, delivered = reports()
    frozen = deepcopy((original, delivered))
    assert classification.deferred_cooldown(original, delivered, NOW)
    assert (original, delivered) == frozen
    assert original["requests"] == 0 and original["daily_budget"]["cooldown_wait_seconds"] == 0
    assert delivered["coverage"] == {"eligible": 2, "measured": 1, "unavailable": 0, "pending": 1}
    assert delivered["job_result"]["status"] == "cooling_down"
    assert not delivered["job_result"]["successful"]
    assert not growth_collection_complete(delivered, NOW)


@pytest.mark.parametrize("persisted,published", [(False, False), (False, True), (True, False)])
def test_delivery_failure_cannot_be_reclassified_as_cooldown(persisted, published):
    original, delivered = reports()
    delivered = stamp_growth_publication(delivered, NOW, state_persisted=persisted, published=published)
    assert not classification.deferred_cooldown(original, delivered, NOW)
    assert delivered["job_result"]["status"] == "failed"
    assert "not_confirmed" in delivered["job_result"]["reason"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(reason="accepted_catalog_changed"),
    lambda d: d.update(reason="source_unavailable"),
    lambda d: d.update(errors=[2]),
    lambda d: d.update(pending=[{"appid": 2, "reason": "transport_or_xml_error"}]),
    lambda d: d.update(pending=[{"appid": 2, "reason": "awaiting_group_resolution"}]),
    lambda d: d.update(pending=[{"appid": 2, "reason": "community_cooldown"}] * 2),
    lambda d: d.update(pending=[]),
    lambda d: d.update(eligible=3),
    lambda d: d.update(eligible_appids=[1, 1]),
    lambda d: d.update(coverage={"eligible": 2, "measured": 1, "unavailable": 0, "pending": 0}),
    lambda d: d.update(next_request_after_taipei="2026-10-16T13:16:00"),
    lambda d: d.update(next_request_after_taipei=NOW.isoformat()),
    lambda d: d.update(collection_date_taipei="2026-10-15"),
    lambda d: d.update(generated_at=(NOW + timedelta(minutes=1)).isoformat()),
    lambda d: d.update(measurements=[{"appid": 1, "followers": -1, "at": NOW.isoformat()}]),
    lambda d: d.update(events=[{"status": "transport_or_xml_error"}]),
    lambda d: d["daily_budget"].update(max_cooldown_wait=3600),
    lambda d: d["daily_budget"].update(cooldown_wait_seconds=False),
    lambda d: d["job_result"].update(successful=True),
    lambda d: d["job_result"].update(reason="state_persistence_not_confirmed"),
])
def test_invalid_original_evidence_is_never_deferred(mutation):
    original, delivered = reports()
    mutation(original)
    assert not classification.deferred_cooldown(original, delivered, NOW)


def test_delivery_reconciliation_and_expired_deadlines_cannot_inherit_old_cooldown():
    original, delivered = reports()
    assert not classification.deferred_cooldown(original, delivered, NOW + timedelta(hours=1))
    delivered["published_eligible_appids"].append(3)
    assert not classification.deferred_cooldown(original, delivered, NOW)


def test_cli_records_deferral_and_preserves_typed_receipt(tmp_path, monkeypatch):
    original, delivered = reports()
    raw, report, output = (tmp_path / name for name in ("original.json", "report.json", "outputs"))
    raw.write_text(json.dumps(original))
    report.write_text(json.dumps(delivered))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(classification, "datetime", SimpleNamespace(now=lambda _: NOW))
    classification.main(["--original", str(raw), "--report", str(report), "--steps-succeeded", "true"])
    result = json.loads(report.read_text())
    assert result["job_result"] == delivered["job_result"]
    assert result["schedule_deferred"]["retry_after"] == original["next_request_after_taipei"]
    assert result["schedule_deferred"]["next_attempt"] == "next_scheduled_six_hour_check"
    assert not result["collection_complete"] and not growth_collection_complete(result, NOW)
    assert output.read_text() == "deferred=true\n"


def test_failed_step_or_missing_report_never_outputs_deferred_true(tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["--original", str(tmp_path / "missing.json"), "--report", str(tmp_path / "missing2.json")]
    classification.main([*args, "--steps-succeeded", "false"])
    assert output.read_text() == "deferred=false\n"
    with pytest.raises(FileNotFoundError):
        classification.main([*args, "--steps-succeeded", "true"])
    assert "deferred=true" not in output.read_text()


def test_duplicate_json_keys_are_rejected_before_skip_output(tmp_path, monkeypatch):
    raw, report, output = (tmp_path / name for name in ("original.json", "report.json", "outputs"))
    raw.write_text('{"reason":"rate_limited","reason":"rate_limited"}')
    report.write_text("{}")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        classification.main(["--original", str(raw), "--report", str(report), "--steps-succeeded", "true"])
    assert not output.exists()


def test_workflow_freezes_original_report_and_skips_only_the_daily_completion_step():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/steam-public-growth.yml").read_text())
    steps = workflow["jobs"]["observe"]["steps"]
    by_id = {step.get("id"): step for step in steps if step.get("id")}
    assert "--max-cooldown-wait 0" in by_id["collect"]["run"]
    assert "cp output/public-growth.json output/growth-collection-original.json" in by_id["collect"]["run"]
    decision = by_id["growth_attempt"]
    assert steps.index(by_id["delivery"]) < steps.index(decision)
    for source in ("collect", "checkpoint", "publish", "delivery"):
        assert f"steps.{source}.outcome == 'success'" in decision["env"]["STEPS_SUCCEEDED"]
    gates = [step for step in steps if step.get("name") == "Require complete growth coverage"]
    assert len(gates) == 1
    assert gates[0]["if"] == "always() && !cancelled() && steps.growth_attempt.outputs.deferred != 'true'"
    assert "growth_collection_complete" in gates[0]["run"] and "raise SystemExit" in gates[0]["run"]
    assert "continue-on-error" not in gates[0]
