from datetime import date

from scripts.update_steam_daily import add_months, merge_segment


def test_add_months_clamps_end_of_month() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_merge_segment_replaces_current_window() -> None:
    existing = [
        {
            "appid": 1,
            "name": "Old In Window",
            "followers": 7000,
            "release_raw": "18 Oct, 2026",
            "release_start": "2026-10-18",
            "release_end": "2026-10-18",
        },
        {
            "appid": 2,
            "name": "Later Game",
            "followers": 9000,
            "release_raw": "18 Jan, 2027",
            "release_start": "2027-01-18",
            "release_end": "2027-01-18",
        },
    ]
    segment = [
        {
            "appid": 3,
            "name": "New In Window",
            "followers": 8000,
            "release_raw": "20 Oct, 2026",
            "release_start": "2026-10-20",
            "release_end": "2026-10-20",
        }
    ]

    result = merge_segment(
        existing,
        segment,
        window_start=date(2026, 9, 18),
        window_end=date(2026, 11, 17),
        today=date(2026, 9, 18),
    )

    assert [row["appid"] for row in result] == [2, 3]
