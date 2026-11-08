"""A reviewed mature game can return; explicit adult evidence still wins."""
from copy import deepcopy

import pytest

from radar_backend.domain.adult_exclusions import excluded_appids_from_document, is_disallowed
from radar_backend.domain.candidate_screening import classify, is_explicit_sex_game
from radar_backend.domain.mature_content_reviews import REVIEWED_NON_EROTIC
from scripts.steam_adult_exclusions import excluded_appids


@pytest.mark.parametrize("appid", sorted(REVIEWED_NON_EROTIC))
def test_review_clears_old_ledger_and_current_descriptor_four(appid):
    document = {"criteria": {"exclude_content_descriptor_ids": [3, 4]},
                "games": [{"appid": appid, "excluded_descriptor_ids": [4]}]}
    before = deepcopy(document)
    assert excluded_appids_from_document(document) == set()
    assert appid not in excluded_appids()
    assert not is_disallowed({"appid": appid, "content_descriptors": {"ids": [1, 4, 5]}}, set())
    candidate = {"appid": appid, "release_precision": "day", "release_start": "2026-11-19"}
    store = {"appid": appid, "release": {"coming_soon_display": "date_full"},
             "content_descriptorids": [1, 4, 5]}
    row, reason = classify(candidate, store)
    assert reason == "eligible" and row["sexual_content_screened"] is True
    assert document == before


@pytest.mark.parametrize("appid", sorted(REVIEWED_NON_EROTIC))
def test_new_adult_only_evidence_overrides_a_mature_game_review(appid):
    document = {"criteria": {"exclude_content_descriptor_ids": [3, 4]},
                "games": [{"appid": appid, "excluded_descriptor_ids": [3, 4]}]}
    assert excluded_appids_from_document(document) == {appid}
    assert is_disallowed({"appid": appid, "content_descriptorids": [1, 3, 4, 5]}, set())
    assert is_explicit_sex_game({"appid": appid, "content_descriptorids": [3, 4]})
    assert is_disallowed({"appid": appid}, {appid})


def test_unreviewed_and_explicit_descriptor_four_games_stay_blocked():
    # This title has descriptor 4 without descriptor 3, but its official notes
    # and game features explicitly describe pornographic scenes.
    appid = 5189130
    assert appid in excluded_appids()
    assert is_disallowed({"appid": appid, "content_descriptorids": [4]}, set())
    assert is_explicit_sex_game({"appid": appid, "content_descriptorids": [4]})


def test_review_does_not_override_new_explicit_primary_description_and_tags():
    assert is_explicit_sex_game({"appid": 4310350, "content_descriptorids": [4],
        "tags": [{"tagid": 12095}, {"tagid": 9130}],
        "basic_info": {"short_description": "An explicit sex game"}})
