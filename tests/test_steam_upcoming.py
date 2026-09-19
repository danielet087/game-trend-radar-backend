from datetime import date, datetime, timezone

from collectors.steam_upcoming import (
    parse_follower_xml,
    parse_release_window,
    parse_search_results_html,
)


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


def test_parse_search_results_html() -> None:
    html = """
    <a href="https://store.steampowered.com/app/123456/Test_Game/"
       data-ds-appid="123456"
       class="search_result_row ds_collapse_flag">
      <div class="search_capsule">
        <img src="https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/123456/capsule.jpg">
      </div>
      <span class="title">Test &amp; Game</span>
      <div class="search_released">18 Sep, 2026</div>
    </a>
    """
    games = parse_search_results_html(html)
    assert len(games) == 1
    game = games[0]
    assert game.appid == 123456
    assert game.name == "Test & Game"
    assert game.release_start == "2026-09-18"
    assert game.store_url == "https://store.steampowered.com/app/123456/"


def test_epoch_release_time_uses_taiwan_calendar_day() -> None:
    # Sep 18 at 17:00 UTC is Sep 19 at 01:00 in Taiwan.
    instant = datetime(2026, 9, 18, 17, tzinfo=timezone.utc)
    seconds = int(instant.timestamp())
    for raw in (seconds, seconds * 1000, str(seconds), str(seconds * 1000)):
        parsed = parse_release_window(raw)
        assert parsed.precision == "day"
        assert parsed.start == date(2026, 9, 19)


def test_offset_timestamp_converts_to_taiwan_without_guessing_hour() -> None:
    instant = parse_release_window("2026-09-18T17:00:00Z")
    assert instant.start == date(2026, 9, 19)
    utc_offset = parse_release_window("2026-09-18T09:00:00-08:00")
    assert utc_offset.start == date(2026, 9, 19)
    # A date-only Steam Store listing does not reveal when the game unlocks.
    # Never automatically add one calendar day to it.
    announced = parse_release_window("18 Sep, 2026")
    assert announced.start == date(2026, 9, 18)
    assert parse_release_window("2026-09-18").start == date(2026, 9, 18)


def test_first_segment_finds_games_missing_from_released_asc(tmp_path) -> None:
    """Steam Price_DESC page 1 may contain first-segment titles absent in Released_ASC."""
    from collectors.steam_upcoming import SteamUpcomingCollector

    def row(appid: int, name: str, released: str) -> str:
        return (
            f'<a class="search_result_row" data-ds-appid="{appid}" '
            f'href="https://store.steampowered.com/app/{appid}/">'
            f'<span class="title">{name}</span>'
            f'<div class="search_released">{released}</div></a>'
        )

    calls: list[str] = []

    class Response:
        def __init__(self, html: str) -> None:
            self.html = html

        def json(self) -> dict:
            return {"results_html": self.html, "total_count": 1}

    def request(url, *, params, limiter):
        sort = params["sort_by"]
        calls.append(sort)
        if sort == "Released_ASC":
            return Response(row(123456, "Another Game", "20 Sep, 2026"))
        if sort == "Price_DESC":
            return Response(row(4115450, "Phantom Blade Zero", "28 Oct, 2026"))
        return Response("")

    collector = SteamUpcomingCollector(
        max_pages=1,
        search_request_interval=0,
        follower_request_interval=0,
        follower_cache_path=tmp_path / "followers.json",
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    collector._request = request
    games = collector.fetch_candidates(
        today=date(2026, 9, 19),
        window_start=date(2026, 9, 19),
        window_end=date(2026, 11, 18),
        segment_index=0,
        segment_anchor=date(2026, 9, 19),
        segment_months=2,
        total_segments=6,
    )
    assert "Price_DESC" in calls
    assert {game.appid for game in games} == {123456, 4115450}
