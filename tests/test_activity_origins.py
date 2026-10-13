"""Publication reasons follow catalog transitions, not current platform tags."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from radar_backend.publication.activity_origins import (
    MAX_ACTIVITY_ORIGINS, catalog_activity_origins, freeze_activity_context,
)

AT = "2026-10-13T07:00:00Z"


def row(appid=1, release="2026-10-20", **extra):
    return {"appid": appid, "release_start": release, **extra}


def catalog(*rows):
    return {"count": len(rows), "games": list(rows)}


def twitch_row(*, checked_at="2026-10-13T06:00:00Z"):
    return row(
        steam_type="game", sexual_content_screened=True, followers=50,
        follower_checked_at="2026-10-13T06:00:00Z", release_precision="day",
        release_display_precision="date_full", release_date_timezone="Asia/Taipei",
        release_end="2026-10-20", release_time_utc="2026-10-20T01:00:00Z",
        release_timestamp_taipei_date="2026-10-20", release_date_conflict=False,
        twitch_admission={"schema_version": 1, "method": "twitch_igdb_external_steam_v1",
            "appid": 1, "twitch_game_id": "101", "igdb_id": "202", "checked_at": checked_at,
            "source_frontend_commit": "a" * 40, "source_enrollment": {
                "source": "igdb_first_release_date", "observed_at": "2026-10-13T05:00:00Z",
                "viewer_count": 7000, "min_viewers": 7000}},
    )


@pytest.mark.parametrize("workflow,expected", [
    ("Steam candidates then Followers", "steam_daily_search"),
    ("Steam official Followers - hourly auto 250", "steam_official_verification"),
    ("Track accepted game growth through release plus 30 days", "steam_catalog_sync"),
    ("Steam candidates then Followers renamed", "steam_catalog_sync"),
])
def test_only_explicit_collection_workflows_identify_daily_search(monkeypatch, workflow, expected):
    monkeypatch.setenv("GITHUB_WORKFLOW", workflow)
    assert freeze_activity_context(now=datetime.fromisoformat(AT.replace("Z", "+00:00"))) == {
        "update_reason": expected, "at": AT,
    }
    assert freeze_activity_context(now=datetime.fromisoformat(AT.replace("Z", "+00:00")),
                                   activity_source="steam_metadata_refresh")["update_reason"] == "steam_metadata_refresh"


def test_only_added_and_release_date_transitions_receive_origins():
    before = catalog(row(1), row(2), row(3))
    after = catalog(row(1, followers=9000), row(2, release="2026-10-21"), row(4))
    old = {"schema_version": 1, "events": [{"appid": 9, "type": "added",
        "date": "2026-10-01", "at": "2026-09-30T01:00:00Z", "update_reason": "steam_metadata_refresh"}]}
    original = deepcopy(old)
    result = catalog_activity_origins(old, before, after,
                                     update_reason="steam_daily_search", at=AT,
                                     payload_revision="b" * 64)
    assert old == original
    assert result["events"][0] == old["events"][0]
    assert result["events"][1:] == [
        {"appid": 2, "type": "release_date", "date": "2026-10-21", "previous_date": "2026-10-20",
         "update_reason": "steam_daily_search", "at": AT, "payload_revision": "b" * 64},
        {"appid": 4, "type": "added", "date": "2026-10-20",
         "update_reason": "steam_daily_search", "at": AT, "payload_revision": "b" * 64},
    ]
    assert catalog_activity_origins(result, after, after,
        update_reason="steam_catalog_sync", at="2026-10-13T08:00:00Z") == result
    assert catalog_activity_origins(result, before, after,
        update_reason="steam_daily_search", at=AT, payload_revision="b" * 64) == result


def test_twitch_identity_is_new_enrollment_evidence_only():
    incoming = twitch_row()
    added = catalog_activity_origins(None, {}, catalog(incoming),
                                    update_reason="steam_official_verification", at=AT)
    assert added["events"][0]["update_reason"] == "twitch_rankings"
    corrected = catalog_activity_origins(added, catalog(row(release="2026-10-19")), catalog(incoming),
        update_reason="steam_metadata_refresh", at=AT)
    assert corrected["events"][-1]["update_reason"] == "steam_metadata_refresh"
    assert corrected["events"][-1]["type"] == "release_date"


@pytest.mark.parametrize("break_proof", [
    lambda value: value["twitch_admission"].update(checked_at="2026-10-13T08:00:00Z"),
    lambda value: value["twitch_admission"]["source_enrollment"].update(viewer_count=6999),
    lambda value: value.update(sexual_content_screened=False),
    lambda value: value.update(release_date_conflict=True),
])
def test_incomplete_or_future_twitch_evidence_does_not_override_actual_publisher(break_proof):
    value = twitch_row()
    break_proof(value)
    result = catalog_activity_origins(None, {}, catalog(value),
        update_reason="steam_daily_search", at=AT)
    assert result["events"][0]["update_reason"] == "steam_daily_search"


def test_append_retains_latest_evidence_with_bounded_history():
    old = {"schema_version": 1, "events": [
        {"appid": aid, "type": "added", "date": "2026-10-20",
         "update_reason": "steam_catalog_sync", "at": AT}
        for aid in range(1, MAX_ACTIVITY_ORIGINS + 1)
    ]}
    result = catalog_activity_origins(old, catalog(), catalog(row(MAX_ACTIVITY_ORIGINS + 1)),
                                     update_reason="steam_daily_search", at=AT)
    assert len(result["events"]) == MAX_ACTIVITY_ORIGINS
    assert result["events"][0]["appid"] == 2
    assert result["events"][-1]["appid"] == MAX_ACTIVITY_ORIGINS + 1
    assert old["events"][0]["appid"] == 1


@pytest.mark.parametrize("invalid", [
    {"schema_version": 2, "events": []},
    {"schema_version": True, "events": []},
    {"schema_version": 1, "events": {}},
    {"schema_version": 1, "events": [{"appid": True, "type": "added", "date": "2026-10-20",
        "update_reason": "steam_daily_search", "at": AT}]},
])
def test_corrupt_existing_sidecar_is_not_discarded(invalid):
    with pytest.raises(ValueError, match="activity origin"):
        catalog_activity_origins(invalid, {}, catalog(row()), update_reason="steam_daily_search", at=AT)


@pytest.mark.parametrize("reason", ["igdb_update", "steam_date_audit", "", "steam_daily_search "])
def test_unknown_producer_reason_cannot_break_other_publishers_sidecar_contract(reason):
    with pytest.raises(ValueError, match="activity update reason"):
        freeze_activity_context(now=datetime(2026, 10, 13, tzinfo=timezone.utc), activity_source=reason)
