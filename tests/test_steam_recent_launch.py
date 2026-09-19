from datetime import date, datetime, timezone
import json
from unittest.mock import patch

from scripts.publish_steam_preview import (
    checked_followers,
    fetch_new_release_appids,
    recent_release,
    run,
)


def test_recent_release_requires_real_follower_count_and_verified_source():
    day = date(2026, 9, 19)
    base = {"release_start": "2026-09-19", "recent_source": "direct_release"}
    assert recent_release({**base, "followers": 3001}, day)
    assert not recent_release({**base, "followers": 3000}, day)
    assert not recent_release({**base, "followers": None}, day)
    assert not recent_release({**base, "followers": 10000, "recent_source": "top_sellers"}, day)
    assert not recent_release({**base, "followers": 5000, "release_start": "2026-08-01"}, day)


def test_existing_pre_release_follower_measurement_is_kept():
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    checkpoint = {"123": {"followers": 5200, "checked_at": "2026-08-01T00:00:00Z"}}
    assert checked_followers(123, checkpoint, {}, now, allow_tracked_history=True) == (
        5200, "2026-08-01T00:00:00Z"
    )
    assert checked_followers(123, checkpoint, {}, now) is None


def test_new_release_search_discovers_games_never_in_upcoming():
    class DummySession:
        pass

    html = (
        '<a class="search_result_row" data-ds-appid="202">'
        '<span class="title">Direct Launch</span>'
        '<div class="search_released">19 Sep, 2026</div></a>'
    )
    with patch(
        "scripts.publish_steam_preview.steam_get",
        return_value={"results_html": html},
    ) as search:
        assert fetch_new_release_appids(DummySession(), date(2026, 9, 19)) == [202]
    assert search.call_args.args[1].endswith("/search/results/")
    assert search.call_args.args[2]["filter"] == "newreleases"


def test_only_real_launches_above_3000_are_in_recent_games(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({
        "games": {"101": {
            "followers": 5200, "checked_at": "2026-09-18T00:00:00Z"
        }},
        "updated_at": "2026-09-19T00:00:00Z",
    }), encoding="utf-8")
    state = tmp_path / "recent_state.json"
    output = tmp_path / "preview.json"

    def details(session, appid, *, language="english"):
        if language != "english":
            return {"name": "繁體"}
        if appid not in (101, 202, 303, 505):
            return None
        coming_soon = appid == 505
        return {
            "type": "game", "name": "Game " + str(appid),
            "release_date": {"date": "19 Sep, 2026", "coming_soon": coming_soon},
        }

    with (
        patch("scripts.publish_steam_preview.app_details", side_effect=details),
        patch("scripts.publish_steam_preview.add_traditional_name"),
        patch("scripts.publish_steam_preview.fetch_new_release_appids", return_value=[101, 202, 303, 505]),
        patch("scripts.publish_steam_preview.fetch_new_release_followers", side_effect=lambda session, appid: {
            202: 3001, 303: 3000, 505: 4000,
        }[appid]) as fetch_follows,
        patch("scripts.publish_steam_preview.time.sleep"),
    ):
        first = run(
            checkpoint_path=checkpoint, output_path=output, recent_state_path=state,
            today=date(2026, 9, 19), delay_seconds=0, follower_interval=0,
        )

    assert first["games"] == []
    assert {game["appid"]: game["recent_source"] for game in first["recent_games"]} == {
        101: "tracked_release", 202: "direct_release"
    }
    assert all(game["followers"] > 3000 for game in first["recent_games"])
    assert fetch_follows.call_count == 3
    private = json.loads(state.read_text(encoding="utf-8"))
    assert private["checked"]["303"]["followers"] == 3000

    # Even if the title drops off Steam's new-releases page, it remains visible
    # until 30 days after its confirmed release.
    with (
        patch("scripts.publish_steam_preview.app_details", side_effect=details),
        patch("scripts.publish_steam_preview.add_traditional_name"),
        patch("scripts.publish_steam_preview.fetch_new_release_appids", return_value=[]),
    ):
        followup = run(
            checkpoint_path=checkpoint, output_path=output, recent_state_path=state,
            today=date(2026, 9, 20), delay_seconds=0, follower_interval=0,
        )
    assert {game["appid"] for game in followup["recent_games"]} == {101, 202}
