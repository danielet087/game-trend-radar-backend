"""New AppIDs use evidence, never a hand-maintained admission exemption."""
from copy import deepcopy

import pytest
from radar_core.domain.mature_content import decide_content
from radar_backend.domain.adult_exclusions import excluded_appids_from_document, is_disallowed
from radar_backend.domain.candidate_screening import classify, is_explicit_sex_game


def ordinary(appid=99000001):
    return {"appid": appid, "release": {"coming_soon_display": "date_full"},
        "content_descriptorids": [1, 4, 5], "basic_info": {"short_description": "A supernatural life simulation."},
        "content_details": {"steam_appid": appid, "short_description": "Build a haunted home and investigate ghosts.",
            "content_descriptors": {"ids": [1, 4, 5], "notes": "Full nudity in optional shower scenes. No sexual acts."}}}


@pytest.mark.parametrize("appid", [2532010, 4310350, 99000001, 99000002])
def test_new_and_previously_reviewed_games_follow_identical_rules(appid):
    store = ordinary(appid)
    before = deepcopy(store)
    row, reason = classify({"appid": appid, "release_precision": "day", "release_start": "2026-11-19"}, store)
    assert reason == "eligible" and row["sexual_content_screened"] is True
    assert not is_disallowed(store, set())
    assert not is_explicit_sex_game(store)
    assert store == before


def test_old_descriptor_four_audit_is_not_a_permanent_exclusion():
    doc = {"criteria": {"exclude_content_descriptor_ids": [3, 4]}, "games": [
        {"appid": 99000001, "excluded_descriptor_ids": [4]},
        {"appid": 99000002, "excluded_descriptor_ids": [3, 4]}]}
    assert excluded_appids_from_document(doc) == {99000002}


def test_descriptor_three_still_overrides_ordinary_context():
    item = ordinary();item["content_descriptorids"].append(3)
    assert is_explicit_sex_game(item)
    assert is_disallowed(item, set())


def test_missing_context_is_retryable_review_then_recovers_without_exemption():
    item = ordinary();details = item.pop("content_details")
    row = {"appid": item["appid"], "release_precision": "day", "release_start": "2026-11-19"}
    assert classify(row, item) == (None, "content_review")
    assert decide_content(item).reason == "official_content_context_unavailable"
    item["content_details"] = details
    assert classify(row, item)[1] == "eligible"


def test_descriptor_four_erotic_features_remain_excluded_for_new_appid():
    item = ordinary();item["content_details"]["about_the_game"] = "Unlock 30 high-quality H-scenes through mission progression."
    assert is_explicit_sex_game(item)
    assert decide_content(item).reason == "primary_erotic_marketing"


def test_mature_ratings_and_negated_erotic_terms_are_not_primary_erotic_marketing():
    item = ordinary();item["content_details"]["about_the_game"] = "A psychological mystery. No H-scenes or pornographic content."
    assert decide_content(item).status == "screened"


def test_current_detail_only_adult_flag_still_overrides_an_ordinary_item():
    item = ordinary();item["content_descriptorids"] = [1, 5]
    item["content_details"]["content_descriptors"]["ids"] = [3]
    assert is_disallowed(item, set())
