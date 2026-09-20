"""No Followers calls in the full-year Store display/sexual eligibility gate."""
import copy

import pytest

from scripts.screen_steam_candidates_before_followers import (
    build_snapshot, classify, is_explicit_sex_game,
)


def candidate(appid=1, day="2026-12-31"):
    return {
        "appid": appid, "name": "Candidate",
        "release_precision": "day",
        "release_start": day, "release_end": day,
        "followers": 5500,
        "capsule_image": "original-capsule-url",
    }


def store(label, ids=(), tags=(), description=""):
    return {
        "release": {"coming_soon_display": label},
        "content_descriptorids": list(ids),
        "tags": [{"tagid": tag} for tag in tags],
        "basic_info": {"short_description": description},
    }


def test_year_quarter_month_timestamps_are_not_real_announced_dates():
    for label in ("date_year", "date_quarter", "date_month", "coming_soon"):
        included, reason = classify(candidate(), store(label))
        assert included is None
        assert reason == label
    assert classify(candidate(), None) == (None, "unavailable")


def test_actual_announced_december_31_is_kept():
    original = candidate()
    copied = copy.deepcopy(original)
    included, status = classify(original, store("date_full", ids=(1, 5)))
    assert status == "eligible"
    assert included["appid"] == original["appid"]
    assert included["release_start"] == "2026-12-31"
    assert included["capsule_image"] == original["capsule_image"]
    assert original == copied
    assert included["release_display_precision"] == "date_full"


def test_exclude_explicit_sex_not_romance_or_ordinary_maturity():
    for ids in ([3], [4], [1, 3, 5]):
        assert is_explicit_sex_game(store("date_full", ids=ids))
        assert classify(candidate(), store("date_full", ids=ids))[1] == "sexual_content"
    for ids in ([1], [2], [5], [1, 2, 5]):
        assert not is_explicit_sex_game(store("date_full", ids=ids))
    assert is_explicit_sex_game(store(
        "date_full", tags=(12095, 6650, 9130), description="An NSFW sex game"
    ))
    assert not is_explicit_sex_game(store(
        "date_full", tags=(12095, 6650), description="Romance and dating"
    ))


def test_snapshot_classifies_complete_original_without_modifying_source():
    original = {"count": 4, "games": [candidate(appid=i) for i in range(1, 5)]}
    preserved = copy.deepcopy(original)
    mapped = {
        1: store("date_full"),
        2: store("date_year"),
        3: store("date_full", ids=(3,)),
    }
    result = build_snapshot(original, mapped)
    assert result["source_count"] == 4
    assert result["count"] == 1
    assert result["excluded"] == 3
    assert result["reasons"] == {
        "date_year": 1, "eligible": 1, "sexual_content": 1,
        "unavailable": 1,
    }
    assert [g["appid"] for g in result["games"]] == [1]
    assert original == preserved
