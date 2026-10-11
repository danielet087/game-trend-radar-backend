"""Growth coverage distinguishes measurements, official absence and retries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from radar_core.jobs import JobResult, JobStatus

from radar_backend.application.growth import collect_observations
from radar_backend.domain import growth
from radar_backend.domain.official_groups import SOURCE
from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)


def game(aid, **changes):
    return {"appid": aid, "followers": 6500, "release_start": "2026-10-11", **changes}


def receipt(status="not_found", **changes):
    value = {"status": status, "checked_at": NOW.isoformat(), "source": SOURCE,
             "attempted": True, "http": 200, "api_result": 42,
             "retry_at": (NOW + timedelta(days=1)).isoformat()}
    if status == "resolved":
        value["api_result"] = 1
        value.pop("retry_at")
    return {**value, **changes}


def batch(*entries):
    return {"schema_version": 1, "requests_this_run": len(entries),
            "results": {str(aid): item for aid, item in entries}}


class Source:
    def __init__(self):
        self.groups = []

    def fetch(self, gid):
        self.groups.append(gid)
        return SimpleNamespace(status="ok", http=200, error_type=None,
                               observed_at=NOW, followers=6700)


def collect(rows, *, checkpoint=None, history=None, resolver=None, source=None,
            clock=None, persist=None, **kwargs):
    checkpoint = checkpoint if checkpoint is not None else {}
    source = source or Source()
    result = collect_observations(
        rows, history or {}, checkpoint, client=source,
        cache=OfficialFollowerCache(checkpoint), cooldown=CooldownStore(checkpoint),
        clock=clock or (lambda: NOW), sleep=lambda _: None, monotonic=lambda: 0,
        persist=persist or (lambda *_: None), group_resolver=resolver, **kwargs,
    )
    return result, source, checkpoint


def delivered(result, at=NOW):
    return stamp_growth_publication(result, at, state_persisted=True, published=True)


def missing_state(aid=1, resolution=None):
    return {"official_growth_group_resolutions": {
        str(aid): {"appid": aid, "group_resolution": resolution or receipt()},
    }}


def test_resolver_repairs_accepted_games_and_accounts_for_verified_absence():
    observed = []
    group = str(GROUP_BASE + 1)

    def resolve(checkpoint, candidates, *, now):
        observed.extend(deepcopy(candidates))
        return batch((1, {"group_resolution": receipt("resolved"), "group_id64": group}),
                     (2, {"group_resolution": receipt()}))

    result, source, state = collect([game(1), game(2)], resolver=resolve)
    assert {row["appid"] for row in observed} == {1, 2}
    assert source.groups == [group]
    assert result["group_resolution_requests"] == 2
    assert result["requests"] == 1
    assert result["measurements"] == [{"appid": 1, "followers": 6700,
                                         "at": "2026-10-11T04:00:00Z", "source": "steam_community"}]
    assert result["unavailable"] == [{"appid": 2, "reason": "official_group_not_found",
                                        "group_resolution": receipt()}]
    assert result["pending"] == result["errors"] == []
    assert result["coverage"] == {"eligible": 2, "measured": 1, "unavailable": 1, "pending": 0}
    assert result["collection_complete"] is True
    assert result["measurement_complete"] is False
    assert set(state["official_growth_observations"]) == {"1"}
    assert state["official_growth_group_resolutions"]["2"]["group_resolution"] == receipt()
    assert result["job_result"]["job"] == "public-growth"
    assert not result["job_result"]["successful"]
    assert growth.growth_collection_complete(delivered(result), NOW)


def test_verified_absence_preserves_all_existing_follower_and_history_evidence():
    old = {"official_followers": 6521, "official_checked_at_taipei": "2026-10-10T04:00:00Z"}
    state = {**missing_state(), "official_results": {"1": deepcopy(old)}}
    history = {"1": {"history": [{"at": "2026-10-10T04:00:00Z", "followers": 6521}]}}
    original_history = deepcopy(history)
    result, source, after = collect([game(1)], checkpoint=state, history=history)
    assert source.groups == []
    assert result["measurements"] == []
    assert "followers" not in result["unavailable"][0]
    assert "at" not in result["unavailable"][0]
    assert after["official_results"]["1"] == old
    assert "official_growth_observations" not in after
    assert history == original_history
    assert growth.growth_collection_complete(delivered(result), NOW)


def test_no_receipt_is_pending_even_when_errors_are_empty_and_publication_succeeded():
    result, source, _ = collect([game(1)])
    assert source.groups == []
    assert result["errors"] == []
    assert result["unavailable"] == []
    assert result["pending"] == [{"appid": 1, "reason": "awaiting_group_resolution"}]
    assert not growth.growth_collection_complete(delivered(result), NOW)


@pytest.mark.parametrize("status,http,api_result,attempted", [
    ("missing_api_key", None, None, False), ("network_error", None, None, True),
    ("api_forbidden", 403, None, True), ("api_rate_limited", 429, None, True),
    ("api_error", 500, None, True), ("invalid_response", 200, None, True),
])
def test_retryable_identity_failures_never_become_terminal_absence(status, http, api_result, attempted):
    evidence = receipt(status, attempted=attempted)
    for key, value in (("http", http), ("api_result", api_result)):
        if value is None:
            evidence.pop(key)
        else:
            evidence[key] = value
    result, _, _ = collect([game(1)], resolver=lambda *_args, **_kwargs: batch(
        (1, {"group_resolution": evidence})))
    assert result["pending"] == [{"appid": 1, "reason": "awaiting_group_resolution"}]
    assert result["unavailable"] == result["measurements"] == []
    assert not result["collection_complete"]


@pytest.mark.parametrize("change,corrupt_state", [
    ({"http": 429}, True), ({"api_result": 1}, True), ({"api_result": True}, True),
    ({"attempted": False}, True), ({"source": "SteamDB"}, True),
    ({"checked_at": "2026-10-11T04:00:00"}, True),
    ({"checked_at": (NOW + timedelta(seconds=1)).isoformat()}, False),
    ({"retry_at": None}, True), ({"retry_at": NOW.isoformat()}, True),
    ({"status": []}, True), ({"status": None}, True),
])
def test_unverified_not_found_receipts_cannot_exclude_a_game(change, corrupt_state):
    assert not growth.group_unavailable(receipt(**change), NOW)
    if corrupt_state:
        source = Source()
        with pytest.raises(ValueError, match="Unverified growth group identity state"):
            collect([game(1)], checkpoint=missing_state(resolution=receipt(**change)), source=source)
        assert source.groups == []
        return
    result, _, _ = collect([game(1)], checkpoint=missing_state(resolution=receipt(**change)))
    assert result["unavailable"] == []
    assert not result["collection_complete"]


def test_expired_absence_rechecks_group_identity_instead_of_skipping_forever():
    expired = receipt(checked_at=(NOW - timedelta(days=2)).isoformat(),
                      retry_at=(NOW - timedelta(days=1)).isoformat())
    state = missing_state(resolution=expired)
    without_retry, _, _ = collect([game(1)], checkpoint=deepcopy(state))
    assert not without_retry["collection_complete"]
    resolved, source, _ = collect([game(1)], checkpoint=state,
        resolver=lambda *_args, **_kwargs: batch((1, {
            "group_resolution": receipt("resolved"), "group_id64": str(GROUP_BASE + 1),
        })))
    assert source.groups == [str(GROUP_BASE + 1)]
    assert resolved["unavailable"] == []
    assert resolved["measurement_complete"]
    assert growth.growth_collection_complete(delivered(resolved), NOW)


def test_a_real_group_id_overrides_a_stale_absence_receipt():
    state = {**missing_state(), "pending_candidates": {
        "1": {"appid": 1, "group_id64": str(GROUP_BASE + 1)},
    }}
    result, source, _ = collect([game(1)], checkpoint=state)
    assert source.groups == [str(GROUP_BASE + 1)]
    assert result["unavailable"] == []
    assert result["measurements"][0]["followers"] == 6700


def test_same_day_real_measurement_is_reused_before_group_resolution():
    measured = game(1, follower_checked_at=NOW.isoformat())
    calls = []
    result, source, _ = collect([measured], resolver=lambda *args, **kwargs: calls.append(args))
    assert calls == source.groups == []
    assert result["reused"] == 1
    assert result["measurements"][0]["at"] == NOW.isoformat()


def test_reused_actual_zero_is_allowed_but_unknown_is_never_converted_to_zero():
    state = {"official_results": {
        "1": {"official_followers": 0, "official_checked_at_taipei": NOW.isoformat()},
    }}
    result, source, _ = collect([game(1), game(2)], checkpoint=state)
    assert result["measurements"] == [{"appid": 1, "followers": 0,
                                         "at": NOW.isoformat(), "source": "steam_community"}]
    assert result["pending"] == [{"appid": 2, "reason": "awaiting_group_resolution"}]
    assert source.groups == []


def test_unknown_followers_remain_eligible_when_shared_twitch_policy_qualifies(monkeypatch):
    row = game(1, followers=None, twitch_admission={"existing_shared_policy": True})
    monkeypatch.setattr(growth, "is_twitch_qualified", lambda item: item == row)
    original = deepcopy(row)
    result, _, _ = collect([row], checkpoint=missing_state())
    assert row == original
    assert result["eligible_appids"] == [1]
    assert result["measurements"] == []
    assert result["unavailable"][0]["appid"] == 1
    assert growth.growth_collection_complete(delivered(result), NOW)
    monkeypatch.setattr(growth, "is_twitch_qualified", lambda _: False)
    assert not growth.eligible(row, NOW.astimezone(growth.TAIPEI).date())


def test_existing_twitch_admission_and_real_low_followers_use_the_shared_rule():
    from tests.test_twitch_public_maintenance import admitted_game
    row = admitted_game()
    original = deepcopy(row)
    result, _, _ = collect([row], checkpoint=missing_state(aid=row["appid"]))
    assert row == original
    assert result["eligible_appids"] == [row["appid"]]
    assert result["measurements"] == []
    assert result["unavailable"][0]["appid"] == row["appid"]


def test_api_evidence_is_persisted_before_follower_requests():
    source = Source()
    saves = []

    def fail(state, report):
        saves.append(deepcopy(state))
        raise OSError("checkpoint unavailable")

    with pytest.raises(OSError, match="checkpoint unavailable"):
        collect([game(1)], source=source, persist=fail,
            resolver=lambda *_args, **_kwargs: batch((1, {
                "group_resolution": receipt("resolved"), "group_id64": str(GROUP_BASE + 1),
            })))
    assert source.groups == []
    assert saves[0]["official_growth_group_resolutions"]["1"]["group_id64"] == str(GROUP_BASE + 1)


@pytest.mark.parametrize("value", [None, {}, {"schema_version": 1, "results": []},
    batch((2, {"group_resolution": receipt()})),
    batch((1, {"group_resolution": receipt(source="unverified")})),
])
def test_bad_group_batch_cannot_claim_success(value):
    with pytest.raises(ValueError):
        collect([game(1)], resolver=lambda *_args, **_kwargs: value)


def test_duplicate_catalog_appids_are_rejected_before_any_request():
    with pytest.raises(ValueError, match="duplicate eligible AppIDs"):
        collect([game(1), game(1)])


@pytest.mark.parametrize("change", [
    {"eligible_appids": [2]}, {"eligible_appids": [True]}, {"eligible_appids": [1, 1]},
    {"eligible": 0}, {"pending": [{"appid": 1, "reason": "awaiting_group_resolution"}]},
    {"measurement_complete": True}, {"collection_complete": False},
    {"coverage_schema_version": None}, {"measurement_complete": None},
    {"coverage": {"eligible": 1, "measured": 1, "unavailable": 0, "pending": 0}},
    {"collection_date_taipei": "2026-10-10"}, {"generated_at": "2026-10-10T04:00:00Z"},
    {"published_eligible_appids": [1, 2]}, {"published_resolvable_appids": [1]},
    {"checkpoint_resolvable_appids": [1]}, {"checkpoint_group_resolutions": {}},
    {"checkpoint_group_resolutions": {"1": receipt("resolved")}},
    {"checkpoint_group_resolutions": {"1": receipt("network_error", http=500)}},
    {"checkpoint_group_resolutions": {"1": receipt(checked_at=(NOW - timedelta(seconds=1)).isoformat())}},
])
def test_completion_rejects_forged_cohort_or_false_publication_coverage(change):
    result, _, _ = collect([game(1)], checkpoint=missing_state())
    assert growth.growth_collection_complete(delivered(result), NOW)
    assert not growth.growth_collection_complete(delivered({**result, **change}), NOW)


def test_unavailable_row_never_counts_as_a_measurement_with_followers_zero():
    result, _, _ = collect([game(1)], checkpoint=missing_state())
    result["unavailable"][0]["followers"] = 0
    assert not growth.growth_collection_complete(delivered(result), NOW)


def test_a_typed_growth_job_cannot_opt_out_of_publication_to_claim_daily_success():
    result, _, _ = collect([game(1)], checkpoint=missing_state())
    result["job_result"] = JobResult(
        job="public-growth", status=JobStatus.COMPLETE, collection_complete=True,
        state_persisted=True, published=False, requires_publication=False,
    ).to_dict()
    assert not growth.growth_collection_complete(result, NOW)


def test_wrong_source_measurement_and_unmeasured_changed_cohort_are_rejected():
    result, _, _ = collect([game(1, follower_checked_at=NOW.isoformat())])
    result["measurements"][0]["source"] = "twitch"
    assert not growth.growth_collection_complete(delivered(result), NOW)


def test_empty_cohort_report_cannot_be_reused_after_taiwan_midnight():
    result, _, _ = collect([])
    assert growth.growth_collection_complete(delivered(result), NOW)
    tomorrow = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
    assert not growth.growth_collection_complete(delivered(result, tomorrow), tomorrow)


def test_collection_crossing_taiwan_midnight_cannot_complete_the_started_day():
    ticks = iter((datetime(2026, 10, 11, 15, 59, 59, tzinfo=timezone.utc),
                  datetime(2026, 10, 11, 16, tzinfo=timezone.utc)))
    result, _, _ = collect([], clock=lambda: next(ticks))
    assert result["collection_date_taipei"] == "2026-10-11"
    assert result["generated_at"] == "2026-10-11T16:00:00+00:00"
    assert not result["collection_complete"]


def test_verified_absence_receipt_does_not_stay_complete_after_its_retry_deadline():
    result, _, _ = collect([game(1)], checkpoint=missing_state())
    assert not growth.growth_collection_complete(delivered(result), NOW + timedelta(days=1))


def test_newer_still_missing_checkpoint_identity_is_valid_but_retryable_is_not():
    result, _, _ = collect([game(1)], checkpoint=missing_state())
    later = NOW + timedelta(seconds=2)
    result["checkpoint_resolvable_appids"] = []
    result["checkpoint_group_resolutions"] = {"1": receipt(checked_at=later.isoformat())}
    assert growth.growth_collection_complete(delivered(result, later), later)
    result["checkpoint_group_resolutions"]["1"] = receipt(
        "network_error", checked_at=later.isoformat())
    assert not growth.growth_collection_complete(delivered(result, later), later)


def test_source_cooldown_remains_pending_while_official_absence_is_accounted():
    state = {**missing_state(aid=2), "pending_candidates": {
        "1": {"appid": 1, "group_id64": str(GROUP_BASE + 1)},
    }, "next_request_after_taipei": (NOW + timedelta(hours=1)).isoformat()}
    result, source, _ = collect([game(1), game(2)], checkpoint=state)
    assert source.groups == []
    assert result["pending"] == [{"appid": 1, "reason": "community_cooldown"}]
    assert [row["appid"] for row in result["unavailable"]] == [2]
    assert not result["collection_complete"]
