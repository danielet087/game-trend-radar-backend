"""Regression checks for bounded, durable Steam initialization chunks."""

from datetime import date

from scripts.update_steam_daily import merge_partial_segment


def game(appid: int, followers: int, day: str = "2027-01-01") -> dict:
    return {
        "appid": appid,
        "name": f"Game {appid}",
        "followers": followers,
        "release_raw": f"{int(day[-2:])} Jan, 2027",
        "release_start": day,
        "release_end": day,
    }


def test_each_50_lookup_batch_keeps_qualified_games_from_previous_batch():
    today = date(2026, 9, 19)
    first = [game(i, 5000 + i) for i in range(1, 6)]
    second = [game(i, 5000 + i) for i in range(6, 11)]
    output = merge_partial_segment([], first, today=today)
    output = merge_partial_segment(output, second, today=today)
    assert {entry["appid"] for entry in output} == set(range(1, 11))


def test_repeated_batch_updates_existing_appid_without_duplicates():
    today = date(2026, 9, 19)
    result = merge_partial_segment(
        [game(1, 6000), game(2, 7000)],
        [game(1, 8000), game(3, 9000)],
        today=today,
    )
    assert {entry["appid"]: entry["followers"] for entry in result} == {
        1: 8000, 2: 7000, 3: 9000,
    }
    assert [entry["appid"] for entry in result] == [3, 1, 2]


def test_released_history_is_retained_without_replacing_unchecked_games():
    today = date(2026, 9, 19)
    result = merge_partial_segment(
        [game(1, 7000, "2026-01-01"), game(2, 6000), game(3, 8000)],
        [game(4, 9000)],
        today=today,
    )
    # Released, already-qualified games stay in the historical calendar;
    # a partial batch must also preserve games that have not been rechecked.
    assert {entry["appid"] for entry in result} == {1, 2, 3, 4}
    assert [entry["appid"] for entry in result] == [4, 3, 1, 2]
    assert next(entry for entry in result if entry["appid"] == 1)["release_start"] == "2026-01-01"
