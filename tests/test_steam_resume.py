from datetime import datetime, timezone

from collectors.steam_upcoming import SteamUpcomingCollector, UpcomingGame


def test_initialization_reuses_saved_follower_even_when_ttl_expired(tmp_path):
    checked_at = "2026-01-01T00:00:00Z"
    collector = SteamUpcomingCollector(
        follower_cache_path=tmp_path / "cache.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        reuse_all_cached_during_initialization=True,
        follower_request_interval=0,
        search_request_interval=0,
    )
    collector.follower_cache = {
        "123": {
            "followers": 6000,
            "checked_at": checked_at,
        }
    }

    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    assert collector._cached_follower_entry(123, now) == (6000, checked_at)


def test_maintenance_still_respects_follower_ttl(tmp_path):
    collector = SteamUpcomingCollector(
        follower_cache_path=tmp_path / "cache.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        reuse_all_cached_during_initialization=False,
        follower_request_interval=0,
        search_request_interval=0,
    )
    collector.follower_cache = {
        "123": {
            "followers": 6000,
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    assert collector._cached_follower_entry(123, now) is None


def test_fresh_request_budget_stops_cleanly_and_saves_checkpoint(tmp_path):
    collector = SteamUpcomingCollector(
        min_followers=0,
        follower_cache_path=tmp_path / "cache.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        checkpoint_every=5,
        reuse_all_cached_during_initialization=True,
        max_fresh_requests_per_run=2,
        follower_request_interval=0,
        search_request_interval=0,
    )
    collector.fetch_followers = lambda appid: appid * 10

    games = [
        UpcomingGame(
            appid=appid,
            name=f"Game {appid}",
            release_raw="20 Sep, 2026",
            release_start="2026-09-20",
            release_end="2026-09-20",
            release_precision="day",
            capsule_image=None,
            store_url=f"https://store.steampowered.com/app/{appid}/",
        )
        for appid in (1, 2, 3)
    ]

    result = collector.qualify(games)

    assert len(result) == 2
    assert collector.fresh_follower_requests == 2
    assert collector.collection_paused_due_to_budget is True
    assert collector.remaining_unchecked_candidates == 1
    assert collector.processed_candidate_count == 2
    assert (tmp_path / "checkpoint.json").exists()
