"""Shared content decisions used by independent collection and publication."""
import unittest
from radar_core.domain.mature_content import decide_content


class MatureContentTests(unittest.TestCase):
    def item(self, appid=99000001):
        return {"appid": appid, "content_descriptorids": [1, 4, 5],
            "basic_info": {"short_description": "A psychological mystery adventure."},
            "content_details": {"steam_appid": appid, "short_description": "Discover the secret of your hometown.",
                "content_descriptors": {"ids": [1, 4, 5], "notes": "Strong Sexual Content, nudity, sex scenes."}}}

    def test_unseen_appids_are_allowed_from_ordinary_context(self):
        for appid in (99000001, 99000002, 99000003):
            with self.subTest(appid=appid):
                self.assertEqual(decide_content(self.item(appid)).status, "screened")

    def test_rating_notes_do_not_define_the_primary_genre(self):
        item = self.item()
        item["content_details"]["content_descriptors"]["notes"] = "High impact violence, sex scenes and drug use"
        self.assertEqual(decide_content(item).status, "screened")

    def test_adult_only_overrides_even_an_ordinary_description(self):
        item = self.item();item["content_descriptorids"].append(3)
        self.assertEqual(decide_content(item).status, "excluded")

    def test_explicit_primary_features_exclude_without_adult_only_flag(self):
        item = self.item();item["content_details"]["about_the_game"] = "Unlock 30 high-quality H-scenes through story progression."
        self.assertEqual(decide_content(item).status, "excluded")

    def test_no_context_is_pending_and_can_recover(self):
        item = self.item();details = item.pop("content_details")
        self.assertEqual(decide_content(item).status, "review")
        item["content_details"] = details
        self.assertEqual(decide_content(item).status, "screened")

    def test_conflicting_identity_and_ambiguous_erotic_tags_require_review(self):
        item = self.item();item["content_details"]["steam_appid"] = 1
        self.assertEqual(decide_content(item).status, "review")
        item = self.item();item["tags"] = [{"tagid": 9130}]
        self.assertEqual(decide_content(item).status, "review")

    def test_negative_marketing_claims_and_soft_romance_remain_ordinary(self):
        for description in ("No H-scenes or pornographic content.", "A romance and dating adventure."):
            item = self.item();item["content_details"]["about_the_game"] = description
            self.assertEqual(decide_content(item).status, "screened")

    def test_another_games_adult_descriptor_never_classifies_this_game(self):
        item = self.item();item["content_details"].update(steam_appid=1, content_descriptors={"ids": [3]})
        self.assertEqual(decide_content(item).status, "review")

    def test_a_name_without_a_game_description_does_not_prove_primary_purpose(self):
        item = self.item();item["content_details"] = {"steam_appid": item["appid"], "name": "A Game"}
        self.assertEqual(decide_content(item).status, "review")
