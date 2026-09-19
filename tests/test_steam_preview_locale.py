from unittest.mock import patch

from collectors.steam_upcoming import parse_release_window
from scripts.publish_steam_preview import (
    add_traditional_name,
    localized_names,
    normalized_metadata,
)


def test_official_traditional_chinese_title_is_preferred():
    en = {"name": "Game in English"}
    zh = {"name": "遊戲繁體名稱"}
    assert localized_names(en, zh) == ("Game in English", "遊戲繁體名稱")


def test_untranslated_or_missing_store_title_falls_back_to_english():
    en = {"name": "Game in English"}
    assert localized_names(en, {"name": "Game in English"}) == ("Game in English", None)
    assert localized_names(en, {"name": "  "}) == ("Game in English", None)
    assert localized_names(en, None) == ("Game in English", None)


def test_localized_name_does_not_change_exact_english_release_date():
    english = {"name": "Game in English", "release_date": {"date": "19 Sep, 2026"}}
    game = normalized_metadata(123, english, 7000, "2026-09-19T00:00:00Z")
    assert game is not None
    assert game["release_start"] == "2026-09-19"
    assert game["name_en"] == "Game in English"
    assert game["name_zh_tw"] is None

    with patch("scripts.publish_steam_preview.app_details", return_value={"name": "遊戲繁體名稱"}) as get:
        with patch("scripts.publish_steam_preview.time.sleep"):
            add_traditional_name(None, 123, game, english, delay_seconds=0)
    get.assert_called_once_with(None, 123, language="tchinese")
    assert game["name_en"] == "Game in English"
    assert game["name_zh_tw"] == "遊戲繁體名稱"
    assert game["release_start"] == "2026-09-19"
