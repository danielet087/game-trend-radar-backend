"""Offline tests: never manufacture Chinese names, never change candidate criteria."""
from copy import deepcopy

from scripts.steam_localized_titles import actual_zh_tw_title, enrich_tw_names


def test_real_store_tw_title_and_bilingual_title():
    assert actual_zh_tw_title("影之刃零") == "影之刃零"
    assert actual_zh_tw_title("《夜城狂想》Nivalis NIghts") == "《夜城狂想》Nivalis NIghts"


def test_store_english_only_does_not_invent_translation():
    assert actual_zh_tw_title("Fable") is None
    assert actual_zh_tw_title("") is None
    assert actual_zh_tw_title(None) is None


def test_enrich_names_preserves_all_original_data_fields():
    rows = [
        {"appid": 4115450, "name": "Phantom Blade Zero",
         "release_start": "2026-10-29", "followers": 109984,
         "capsule_image": "unchanged"},
        {"appid": 2769570, "name": "Fable",
         "release_start": "2027-02-24", "followers": 140298},
    ]
    prior = deepcopy(rows)
    result = enrich_tw_names(rows, {
        4115450: "影之刃零",
        2769570: "Fable",
    })
    assert result == {
        "total": 2, "official_zh_tw": 1,
        "english_fallback": 1, "store_not_returned": 0,
    }
    assert rows[0]["name_en"] == "Phantom Blade Zero"
    assert rows[0]["name_zh_tw"] == "影之刃零"
    assert rows[1]["name_en"] == "Fable"
    assert rows[1].get("name_zh_tw") is None
    for current, previous in zip(rows, prior):
        assert {key: current[key] for key in previous} == previous


def test_keep_previous_reviewed_chinese_title_if_store_falls_back_to_english():
    rows = [{"appid": 101, "name": "English",
             "name_zh_tw": "已確認中文名稱"}]
    result = enrich_tw_names(rows, {101: "English"})
    assert rows[0]["name_zh_tw"] == "已確認中文名稱"
    assert result["official_zh_tw"] == 1


def test_official_simplified_marketing_title_has_separate_traditional_display():
    from scripts.steam_localized_titles import add_traditional_display_names
    game = {
        "appid": 4019220,
        "name_en": "Dressmaker",
        "name_zh_cn": "针影裁梦",
        "language_support": {"tchinese": False, "schinese": True, "english": True},
    }
    add_traditional_display_names(game)
    assert game["name_zh_cn_traditional"] == "針影裁夢"
    assert game["name_zh_cn"] == "针影裁梦"
    assert game["language_support"]["tchinese"] is False
    assert game["language_support"]["schinese"] is True
    title = {"appid": 4094660, "name_zh_cn": "她在时间之外"}
    add_traditional_display_names(title)
    assert title["name_zh_cn_traditional"] == "她在時間之外"
