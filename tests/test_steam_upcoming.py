from datetime import date

from collectors.steam_upcoming import SteamUpcomingCollector, parse_follower_xml, parse_release_window


def test_parse_exact_day_day_first() -> None:
    result = parse_release_window("18 Sep, 2026")
    assert result.start == date(2026, 9, 18)
    assert result.end == date(2026, 9, 18)
    assert result.precision == "day"


def test_parse_exact_day_month_first() -> None:
    result = parse_release_window("September 18, 2026")
    assert result.start == date(2026, 9, 18)
    assert result.end == date(2026, 9, 18)


def test_parse_month_window() -> None:
    result = parse_release_window("Feb 2027")
    assert result.start == date(2027, 2, 1)
    assert result.end == date(2027, 2, 28)
    assert result.precision == "month"


def test_parse_quarter_window() -> None:
    result = parse_release_window("Q4 2026")
    assert result.start == date(2026, 10, 1)
    assert result.end == date(2026, 12, 31)
    assert result.precision == "quarter"


def test_parse_year_window() -> None:
    result = parse_release_window("2027")
    assert result.start == date(2027, 1, 1)
    assert result.end == date(2027, 12, 31)
    assert result.precision == "year"


def test_unknown_release_date_is_not_assumed() -> None:
    result = parse_release_window("Coming Soon")
    assert result.start is None
    assert result.end is None
    assert result.precision == "unknown"


def test_parse_follower_xml_group_details() -> None:
    xml = "<?xml version='1.0'?><memberList><groupDetails><memberCount>12,345</memberCount></groupDetails></memberList>"
    assert parse_follower_xml(xml) == 12345


def test_item_mapping() -> None:
    game = SteamUpcomingCollector._item_to_game(
        {
            "id": 123456,
            "name": "Test Game",
            "logo": "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/123456/capsule.jpg",
            "release_date": "18 Sep, 2026",
        }
    )
    assert game is not None
    assert game.appid == 123456
    assert game.release_start == "2026-09-18"
    assert game.store_url == "https://store.steampowered.com/app/123456/"
