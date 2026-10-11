"""Exercise growth's real API composition with offline official responses."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.jobs.growth import collect

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)


def inputs(root):
    game = {"appid": 4739040, "followers": 17796,
            "release_start": "2026-10-11",
            "follower_checked_at": "2026-09-18T22:40:40Z"}
    (root / "catalog.json").write_text(json.dumps({"count": 1, "games": [game]}))
    path = root / "checkpoint.json"
    path.write_text(json.dumps({"official_results": {"4739040": {
        "official_followers": 17796, "official_checked_at_taipei": game["follower_checked_at"]}},
        "pending_candidates": {}, "unrelated": {"preserved": True}}))
    return path


class Session:
    def __init__(self, result):
        self.headers = {}
        self.calls = []
        self.result = result

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "ResolveVanityURL" in url:
            return SimpleNamespace(status_code=200, headers={},
                                   json=lambda: {"response": self.result})
        gid = GROUP_BASE + 4739040
        return SimpleNamespace(status_code=200, headers={}, content=(
            f"<memberList><groupID64>{gid}</groupID64>"
            "<memberCount>18200</memberCount></memberList>").encode())


def test_real_composition_recovers_identity_for_already_measured_legacy_game(tmp_path):
    checkpoint = inputs(tmp_path)
    session = Session({"success": 1, "steamid": str(GROUP_BASE + 4739040)})
    result = collect(tmp_path, tmp_path / "report.json", checkpoint_path=checkpoint,
                     now=NOW, session=session, sleep=lambda _: None, api_key="offline-key")
    assert len(session.calls) == 2
    assert session.calls[0][1]["headers"] == {"x-webapi-key": "offline-key"}
    assert session.calls[0][1]["params"]["vanityurl"] == "4739040"
    assert result["group_resolution_requests"] == 1
    assert result["requests"] == 1
    assert result["measurements"][0]["followers"] == 18200
    assert result["collection_complete"] and result["measurement_complete"]
    saved = json.loads(checkpoint.read_text())
    assert saved["official_results"]["4739040"]["official_followers"] == 17796
    assert saved["official_growth_observations"]["4739040"]["official_followers"] == 18200
    assert saved["unrelated"] == {"preserved": True}


def test_not_found_is_recorded_without_fabricating_or_replacing_followers(tmp_path):
    checkpoint = inputs(tmp_path)
    session = Session({"success": 42})
    result = collect(tmp_path, tmp_path / "report.json", checkpoint_path=checkpoint,
                     now=NOW, session=session, sleep=lambda _: None, api_key="offline-key")
    assert len(session.calls) == 1
    assert result["measurements"] == []
    assert result["unavailable"][0]["appid"] == 4739040
    assert result["collection_complete"] and not result["measurement_complete"]
    saved = json.loads(checkpoint.read_text())
    assert saved["official_results"]["4739040"]["official_followers"] == 17796
    assert not saved.get("official_growth_observations")
    assert saved["official_growth_group_resolutions"]["4739040"]["group_resolution"]["api_result"] == 42


def test_missing_key_performs_no_api_or_xml_request_and_remains_retryable(tmp_path):
    checkpoint = inputs(tmp_path)
    session = Session({"success": 42})
    result = collect(tmp_path, tmp_path / "report.json", checkpoint_path=checkpoint,
                     now=NOW, session=session, sleep=lambda _: None, api_key="")
    assert session.calls == []
    assert result["unavailable"] == [] and result["measurements"] == []
    assert not result["collection_complete"]
    assert result["pending"][0]["appid"] == 4739040
