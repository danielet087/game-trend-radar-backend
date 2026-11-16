"""The daily gate must accept Twitch-admitted games with unknown Followers."""
import json

import pytest

from scripts.validate_daily_results import publicly_qualified, validate


def twitch_game(**changes):
    game = {
        "appid": 123, "steam_type": "game", "sexual_content_screened": True,
        "release_start": "2026-10-02", "release_end": "2026-10-02", "release_precision": "day",
        "release_display_precision": "date_full", "release_date_timezone": "Asia/Taipei",
        "release_time_utc": "2026-10-02T07:00:00Z", "release_timestamp_taipei_date": "2026-10-02",
        "followers": None, "follower_checked_at": None, "follower_source": None,
        "twitch_admission": {
            "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 123,
            "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
            "source_frontend_commit": "a" * 40,
            "source_enrollment": {"source": "igdb_first_release_date", "observed_at": "2026-10-01T08:00:00Z",
                                  "viewer_count": 8000, "min_viewers": 7000},
        },
    }
    return {**game, **changes}


def write_batch(tmp_path, games, name="steam_upcoming.json", **initialization):
    path = tmp_path / name
    path.write_text(json.dumps({"count": len(games), "games": games, "initialization": initialization}))
    return path


PREFILTER = {"prefilter_next_index": 200, "prefilter_screened_count": 200,
             "last_attempt": {"phase": "prefilter", "fresh_follower_requests": 0, "candidate_count": 3768}}


def test_unknown_followers_pass_only_with_a_twitch_admission():
    assert publicly_qualified(twitch_game())
    assert publicly_qualified({"appid": 1, "followers": 5000})
    assert not publicly_qualified({"appid": 2, "followers": None})
    assert not publicly_qualified({"appid": 3, "followers": 4999})


def test_prefilter_batch_with_a_twitch_game_of_unknown_followers(tmp_path):
    batch = write_batch(tmp_path, [twitch_game(), {"appid": 9, "followers": 8000}], prefilter_next_index=200)
    assert "Steam XML queries: 0" in validate(PREFILTER, batch)
    bad = write_batch(tmp_path, [{"appid": 7, "followers": None}], "bad.json", prefilter_next_index=200)
    with pytest.raises(AssertionError, match=r"\[7\]"):
        validate(PREFILTER, bad)
    queried = {**PREFILTER, "last_attempt": {**PREFILTER["last_attempt"], "fresh_follower_requests": 1}}
    with pytest.raises(AssertionError, match="NEVER query"):
        validate(queried, batch)


def test_followers_batch_rules(tmp_path):
    state = {"last_attempt": {"phase": "followers", "fresh_follower_requests": 12, "start_index": 3, "next_index": 5}}
    batch = write_batch(tmp_path, [twitch_game()], prefilter_complete=True, verified_priority_count=5, days_scanned=365)
    assert "12 new, shortlisted 3->5" in validate(state, batch)
    with pytest.raises(AssertionError):
        validate({"last_attempt": {**state["last_attempt"], "fresh_follower_requests": 51}}, batch)


def test_a_run_without_a_batch_is_discovery_only(tmp_path):
    assert "discovery only" in validate(PREFILTER, tmp_path / "missing.json")
    assert "no new XML batch" in validate({"last_attempt": {"phase": "date_precision"}}, tmp_path / "missing.json")
