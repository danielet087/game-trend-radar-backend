from datetime import date

from scripts.steam_release_dates import corrected_games, resolve_release_date


def test_dressmaker_reported_taiwan_store_date_without_fake_unlock_time():
    result = resolve_release_date(4019220, "21 Sep, 2026")
    assert result["release_start"] == "2026-09-22"
    assert result["release_end"] == "2026-09-22"
    assert result["release_raw"] == "21 Sep, 2026"
    assert result["release_time_utc"] is None
    assert result["release_date_timezone"] == "Asia/Taipei"
    assert result["release_date_basis"] == "steam_tw_storefront_date_user_reported"


def test_unverified_release_dates_do_not_shift():
    other = resolve_release_date(99999, "21 Sep, 2026")
    assert other["release_start"] == "2026-09-21"
    assert other["release_time_utc"] is None


def test_changed_store_date_disables_stale_reported_date():
    updated = resolve_release_date(4019220, "25 Sep, 2026")
    assert updated["release_start"] == "2026-09-25"
    assert updated["release_date_basis"] == "steam_store_announced_date"


def test_structured_timestamp_preferred_when_available():
    release = resolve_release_date(
        99999, "21 Sep, 2026",
        detail={"timestamp": "2026-09-21T16:00:00Z"},
    )
    assert release["release_start"] == "2026-09-22"
    assert release["release_date_basis"] == "steam_structured_release_time"


def test_cached_official_data_is_corrected_without_rechecking_followers():
    original = [{"appid": 4019220, "release_raw": "21 Sep, 2026",
                 "release_start": "2026-09-21", "release_end": "2026-09-21",
                 "followers": 12629}]
    fixed = corrected_games(original)
    assert original[0]["release_start"] == "2026-09-21"
    assert fixed[0]["release_start"] == "2026-09-22"
    assert fixed[0]["followers"] == 12629
