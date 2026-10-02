from copy import deepcopy
from datetime import date
import json
from unittest.mock import patch

from scripts.collect_public_growth import eligible
from scripts.dispatch_content_refresh_events import dispatch, select_games


def admitted_game():
    return {
        "appid": 123, "name": "Twitch discovery", "followers": 812,
        "follower_checked_at": "2026-10-02T06:00:00Z",
        "steam_type": "game", "sexual_content_screened": True,
        "release_start": "2026-10-02", "release_end": "2026-10-02",
        "release_precision": "day", "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei", "release_time_utc": "2026-10-02T07:00:00Z",
        "twitch_admission": {
            "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 123,
            "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
            "source_frontend_commit": "a" * 40,
            "source_enrollment": {"source": "igdb_first_release_date",
                "observed_at": "2026-10-01T08:00:00Z", "viewer_count": 8000, "min_viewers": 7000},
        },
    }


def test_growth_keeps_real_low_follower_source_and_existing_30_day_window():
    row = admitted_game()
    assert eligible(row, date(2026, 10, 2))
    assert eligible(row, date(2026, 11, 1))
    assert not eligible(row, date(2026, 11, 2))
    del row["twitch_admission"]
    assert not eligible(row, date(2026, 10, 2))


def test_refresh_preserves_source_but_rejects_unverified_low_followers():
    good = admitted_game()
    ordinary = deepcopy(good); ordinary.pop("twitch_admission"); ordinary["appid"] = 124
    malformed = deepcopy(good); malformed["twitch_admission"]["source_enrollment"]["viewer_count"] = 6999
    selected = select_games({"games": [good, ordinary, malformed]}, None)
    assert len(selected) == 1
    assert selected[0]["followers"] == 812
    assert selected[0]["twitch_admission"] == good["twitch_admission"]


def test_refresh_event_delivers_proof_and_actual_zero_without_threshold_fallback():
    row = select_games({"games": [{**admitted_game(), "followers": 0}]}, None)[0]
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return False
    with patch("scripts.dispatch_content_refresh_events.urllib.request.urlopen", return_value=Response()) as send:
        dispatch("test-token", row, "test-refresh")
    event = json.loads(send.call_args.args[0].data)
    assert event["event_type"] == "steam_game_refresh"
    assert event["client_payload"]["official_followers"] == 0
    assert event["client_payload"]["twitch_admission"] == row["twitch_admission"]
