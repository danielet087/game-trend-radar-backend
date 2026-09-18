from collectors.twitch_live import aggregate_streams, build_tracked_game_summary, TwitchGame


def test_aggregate_streams() -> None:
    streams = [
        {
            "game_id": "1",
            "game_name": "Game A",
            "viewer_count": 120,
            "language": "zh",
        },
        {
            "game_id": "1",
            "game_name": "Game A",
            "viewer_count": 80,
            "language": "en",
        },
        {
            "game_id": "2",
            "game_name": "Game B",
            "viewer_count": 300,
            "language": "ja",
        },
    ]

    rows = aggregate_streams(streams)
    assert rows[0]["game_id"] == "2"
    assert rows[0]["viewer_count"] == 300
    assert rows[1]["streamer_count"] == 2
    assert rows[1]["viewer_count"] == 200
    assert rows[1]["language_streamers"] == {"zh": 1, "en": 1}


def test_tracked_game_summary_keeps_unmatched_games() -> None:
    steam_games = [
        {"appid": 10, "name": "Game A"},
        {"appid": 20, "name": "Game Missing"},
    ]
    twitch_games = {
        "game a": TwitchGame(
            id="1",
            name="Game A",
            box_art_url="https://example.test/a.jpg",
            igdb_id="99",
        )
    }
    streams = [
        {
            "game_id": "1",
            "game_name": "Game A",
            "viewer_count": 42,
            "language": "zh",
        }
    ]

    rows = build_tracked_game_summary(steam_games, twitch_games, streams)
    assert len(rows) == 2

    matched = next(row for row in rows if row["steam_appid"] == 10)
    missing = next(row for row in rows if row["steam_appid"] == 20)

    assert matched["matched"] is True
    assert matched["streamer_count"] == 1
    assert matched["viewer_count"] == 42

    assert missing["matched"] is False
    assert missing["twitch_game_id"] is None
    assert missing["viewer_count"] == 0
