from datetime import date

from scripts.steam_release_dates import (
    STORE_BROWSE_URL, corrected_games, fetch_store_browse_releases,
    resolve_release_date, resolved_store_date,
)


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


def test_real_steam_browse_timestamp_converts_dressmaker_to_taiwan_day():
    # Probed from public IStoreBrowseService/GetItems with TW country.
    release = resolved_store_date(
        4019220, "21 Sep, 2026",
        {"steam_release_date": 1790006400, "release_time_source": STORE_BROWSE_URL},
    )
    assert release["release_start"] == "2026-09-22"
    assert release["release_time_utc"] == "2026-09-21T16:00:00Z"
    assert release["release_date_basis"] == "steam_store_browse_release_time"
    assert release["release_time_source"] == STORE_BROWSE_URL


def test_browse_timestamp_overrides_date_only_for_any_appid():
    game = resolved_store_date(
        123456, "22 Sep, 2026",
        {"steam_release_date": 1790096100, "release_time_source": STORE_BROWSE_URL},
    )
    assert game["release_start"] == "2026-09-23"


def test_invalid_browse_time_preserves_announced_date():
    old = resolve_release_date(123, "22 Sep, 2026")
    result = resolved_store_date(123, "22 Sep, 2026",
                                 {"steam_release_date": 999, "release_time_source": STORE_BROWSE_URL})
    assert result["release_start"] == old["release_start"]


def test_bulk_correction_preserves_followers_without_any_extra_lookup():
    records = [{"appid": 555, "release_raw": "22 Sep, 2026",
                "release_start": "2026-09-22", "release_end": "2026-09-22",
                "followers": 12543}]
    fixed = corrected_games(records, {
        555: {"steam_release_date": 1790096100,
              "release_time_source": STORE_BROWSE_URL}
    })
    assert fixed[0]["release_start"] == "2026-09-23"
    assert fixed[0]["followers"] == 12543
    assert fixed[0]["release_date_basis"] == "steam_store_browse_release_time"


def test_batched_browse_accepts_only_plausible_release_timestamps():
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"response": {"store_items": [
                {"appid": 4019220, "release": {"steam_release_date": 1790006400,
                                               "is_coming_soon": True}},
                {"appid": 999, "release": {"steam_release_date": 0}},
            ]}}
    class Session:
        def __init__(self): self.params = None; self.calls = 0
        def get(self, url, *, params, timeout):
            assert url == STORE_BROWSE_URL
            assert timeout == 25
            self.params = params
            self.calls += 1
            return Response()

    session=Session()
    import json
    releases=fetch_store_browse_releases(session,[4019220,999],request_interval=0)
    assert list(releases) == [4019220]
    assert releases[4019220]["steam_release_date"] == 1790006400
    payload=json.loads(session.params["input_json"])
    assert payload["context"]["country_code"] == "TW"
    assert payload["data_request"]["include_release"] is True
    assert session.calls == 1
